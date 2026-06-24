'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';
import { useVisibleInterval } from '@/lib/useVisibleInterval';
import StrategyPreview from './StrategyPreview';
import { STRATEGY_FLAGS, type StratFlag } from '@/lib/strategyFlags';

/** Minutes left on a Risk-Guard cooldown from its ISO end timestamp. */
function guardMinsLeft(iso?: string | null): number {
  if (!iso) return 0;
  const ms = new Date(iso).getTime() - Date.now();
  return ms > 0 ? Math.ceil(ms / 60000) : 0;
}

interface Props {
  pair: string;
  mode: 'paper' | 'live';
  paperBalance?: number;   // paper engine balance passed from parent
  onBotCreated: () => void;
}

type Category = 'all' | 'grid' | 'ai' | 'dca';

interface StrategyCard {
  id: number | null;
  name: string;
  label: string;
  description: string;
  category: Category;
  tags: string[];
  icon: string;
  users: number;
  profitPct: number;
  isNew?: boolean;
}

const BUILT_IN_BOTS: StrategyCard[] = [
  {
    id: null, name: 'SpotGrid', label: 'Spot Grid', icon: '📊',
    description: 'Kill volatility by selling high and buying low.',
    category: 'grid', tags: ['Volatile Markets'], users: 9893282, profitPct: 943.72,
  },
  {
    id: null, name: 'FuturesGrid', label: 'Futures Grid', icon: '10X',
    description: 'Long or short to profit from market trends.',
    category: 'grid', tags: ['Advanced', 'Bear Markets'], users: 3582741, profitPct: 520.31,
  },
  {
    id: null, name: 'MarginGrid', label: 'Margin Grid', icon: '⚖️',
    description: 'Kill volatility by selling high and buying low.',
    category: 'grid', tags: ['Advanced', 'Volatile Markets'], users: 282428, profitPct: 146.14,
  },
  {
    id: null, name: 'InfinityGrid', label: 'Infinity Grid', icon: '∞',
    description: 'Bullish volatility killer.',
    category: 'grid', tags: ['Volatile Markets'], users: 609954, profitPct: 293.33,
  },
  {
    id: null, name: 'SimpleTargetStrategy', label: 'AI Futures Trend', icon: '🤖',
    description: 'Automatically captures market trends, optimizing profits during consistent uptrends or downtrends.',
    category: 'ai', tags: ['Beginner', 'Bull Markets'], users: 344814, profitPct: 1696.62, isNew: true,
  },
  {
    id: null, name: 'MissCandleLongStrategy', label: 'DualFutures AI', icon: '🔄',
    description: 'Profit from long and short positions, perfect for volatile markets.',
    category: 'ai', tags: ['Beginner', 'Volatile Markets'], users: 381619, profitPct: 1269.68,
  },
  {
    id: null, name: 'DcaAccumulationStrategy', label: 'DCA', icon: '📈',
    description: 'Make profits from regular investment.',
    category: 'dca', tags: ['Bull Markets'], users: 120500, profitPct: 85.4,
  },
  {
    id: null, name: 'RsiBollingerStrategy', label: 'Smart Rebalance', icon: '⚡',
    description: 'An investment portfolio that spreads risks in the long-term.',
    category: 'dca', tags: ['Bull Markets'], users: 45200, profitPct: 62.1,
  },
];

function formatUsers(n: number) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return Math.floor(n).toLocaleString();
  return String(n);
}

export default function BotPanel({ pair, mode, paperBalance, onBotCreated }: Props) {
  const [category, setCategory] = useState<Category>('all');
  const [strategies, setStrategies] = useState<any[]>([]);
  const [selectedBot, setSelectedBot] = useState<StrategyCard | null>(null);
  const [viewingBotId, setViewingBotId] = useState<number | null>(null);
  const [leadStatus, setLeadStatus] = useState<{ connected: boolean; balance?: number; equity?: number; reason?: string } | null>(null);
  const [runningBots, setRunningBots] = useState<any[]>([]);
  const [mainEngine, setMainEngine] = useState<any>(null);
  // Collapse toggles — when the user is focused on Active Bots they don't
  // want Recent Bots + the strategy library taking up half the panel.
  // Library defaults to collapsed because most users come back to monitor
  // bots they've already started — the conditional layout below switches
  // it to EXPANDED at the top when no active bots exist (otherwise the
  // panel would be a wall of empty space with one tiny chevron).
  const [recentCollapsed, setRecentCollapsed] = useState(false);
  const [libraryCollapsed, setLibraryCollapsed] = useState(true);
  // Tracks whether the FIRST /bots fetch has returned. Without this the
  // panel flashes "no active bots → Strategy Library at top" for the
  // ~500ms before the network responds, then shuffles when the bot
  // cards finally appear. We render a stable loading state until the
  // initial fetch completes.
  const [initialLoaded, setInitialLoaded] = useState(false);
  // Derived: do we have anything to show in the Active Bots area?
  const hasActiveContent = runningBots.filter(b => b.is_running).length > 0 || !!mainEngine;
  // When the panel first mounts AND the initial load has happened with
  // no active bots, force-expand the library so the user can immediately
  // pick a strategy to start. Avoiding the pre-load auto-expand prevents
  // the flash where library opens then collapses once bots arrive.
  useEffect(() => {
    if (initialLoaded && !hasActiveContent) setLibraryCollapsed(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialLoaded]);

  const refreshBots = useCallback(() => {
    api.futures.bots.list(mode)
      .then(d => setRunningBots(d.bots || []))
      .catch(() => {})
      .finally(() => setInitialLoaded(true));
    api.futures.status().then(d => setMainEngine(d?.running ? d : null)).catch(() => setMainEngine(null));
  }, [mode]);

  useEffect(() => {
    api.strategy.list().then(d => setStrategies(d.strategies || [])).catch(() => {});
    api.futures.leadTradingStatus().then(d => setLeadStatus(d)).catch(() => {});
    refreshBots();
  }, [refreshBots]);
  // Visibility-aware: 5s refresh only while the tab is on screen.
  useVisibleInterval(refreshBots, 5000);

  const userStrategyCards: StrategyCard[] = strategies
    .filter(s => !BUILT_IN_BOTS.find(b => b.name === s.name))
    .map(s => ({
      id: s.id,
      name: s.name,
      label: s.name.replace(/([A-Z])/g, ' $1').trim(),
      description: s.description || 'Custom user strategy — generates signals for futures lead trading.',
      category: 'ai' as Category,
      tags: s.is_template ? ['Template', 'Lead Trading'] : ['My Strategy', 'Lead Trading'],
      icon: '🎯',
      users: 0,
      profitPct: 0,
    }));

  const allBots: StrategyCard[] = [
    ...userStrategyCards,
    ...BUILT_IN_BOTS,
  ];

  const filtered = category === 'all' ? allBots : allBots.filter(b => b.category === category);

  if (viewingBotId) {
    return (
      <BotDetailView
        botId={viewingBotId}
        onBack={() => { setViewingBotId(null); refreshBots(); }}
        onStop={async () => {
          const res = await api.futures.bots.stop(viewingBotId);
          if (res.winding_down) {
            refreshBots();
          } else {
            setViewingBotId(null);
            refreshBots();
          }
        }}
      />
    );
  }

  if (selectedBot) {
    return (
      <BotCreateFlow
        bot={selectedBot}
        pair={pair}
        mode={mode}
        paperBalance={paperBalance}
        strategies={strategies}
        onBack={() => { setSelectedBot(null); refreshBots(); }}
        onCreated={() => { onBotCreated(); refreshBots(); }}
      />
    );
  }

  const categories: { key: Category; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'grid', label: 'Grid Strategy' },
    { key: 'ai', label: 'AI-Powered' },
    { key: 'dca', label: 'Cost-Averaging' },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Account status banner — mirror the Manual tab's pattern so
          paper bots show "Paper Trading Account" instead of confusingly
          claiming "Lead Trading Connected" (paper doesn't touch Lead
          Trading at all). Live bots show the real Lead Trading state. */}
      <div className={`flex items-center justify-between px-3 py-2 text-xs font-bold border-b ${
        mode === 'paper'
          ? 'bg-indigo-500/20 border-indigo-500/30'
          : leadStatus?.connected
            ? 'bg-emerald-500/20 border-emerald-500/30'
            : 'bg-amber-500/20 border-amber-500/30'
      }`}>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full shrink-0 ${
            mode === 'paper'
              ? 'bg-indigo-400'
              : leadStatus?.connected
                ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]'
                : 'bg-amber-400'
          }`} />
          <span className={
            mode === 'paper' ? 'text-indigo-300'
              : leadStatus?.connected ? 'text-emerald-300' : 'text-amber-300'
          }>
            {mode === 'paper'
              ? `Paper Trading Account`
              : leadStatus?.connected
                ? `Lead Trading Connected • ${(leadStatus.balance ?? 0).toFixed(2)} USDT`
                : 'Lead Trading: Not Connected'}
          </span>
        </div>
        {mode === 'paper' ? (
          <span className="text-[11px] text-indigo-300 font-medium">
            {(paperBalance ?? 1000).toFixed(2)} USDT
          </span>
        ) : (
          <span className="text-[10px] text-amber-300 font-medium">Live Mode</span>
        )}
      </div>

      {/* Initial loading state — first paint of the panel runs while the
          /bots fetch is still in flight. Showing the real "no active bots
          → Strategy Library expanded" layout in that window would flash
          and then re-shuffle when the real data arrives a few hundred ms
          later. Render a thin shimmer in the Active Bots slot until the
          first response lands. */}
      {!initialLoaded && (
        <div className="px-3 py-2 border-b border-white/[0.06] flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between mb-1.5 shrink-0">
            <p className="text-[10px] text-emerald-400 font-bold">Active Bots</p>
            <span className="flex items-center gap-1.5 text-[10px] text-slate-400">
              <span className="w-2 h-2 rounded-full bg-emerald-400/60 animate-pulse" />
              Loading bots…
            </span>
          </div>
          <div className="space-y-1.5">
            {[0, 1].map(i => (
              <div key={i} className="p-2.5 rounded-lg bg-[#1e222d] border border-white/[0.05] animate-pulse">
                <div className="h-3 w-32 rounded bg-slate-700/60 mb-2" />
                <div className="grid grid-cols-3 gap-2">
                  <div className="h-8 rounded bg-slate-800" />
                  <div className="h-8 rounded bg-slate-800" />
                  <div className="h-8 rounded bg-slate-800" />
                </div>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-slate-500 text-center mt-2 italic">
            Fetching your active strategies — this usually takes 1-2 seconds…
          </p>
        </div>
      )}

      {/* Running Bots — flex-1 so it takes ALL space between the header and
          the collapsed footer sections (Recent Bots + Strategy Library).
          User asked for Active Bots to be the primary thing they see. */}
      {initialLoaded && (runningBots.filter(b => b.is_running).length > 0 || mainEngine) && (
        <div className="px-3 py-2 border-b border-white/[0.06] flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between mb-1.5 shrink-0">
            <p className="text-[10px] text-emerald-400 font-bold">Active Bots ({runningBots.filter(b => b.is_running).length + (mainEngine ? 1 : 0)})</p>
            <div className="flex items-center gap-2">
              {mode === 'paper' && (
                <button
                  onClick={async () => {
                    const input = window.prompt(
                      "Add virtual USDT to your paper wallet.\nEnter amount (1 to 1,000,000):",
                      "1000",
                    );
                    if (!input) return;
                    const amt = Number(input);
                    if (!Number.isFinite(amt) || amt < 0.01 || amt > 1_000_000) {
                      alert("Amount must be between 0.01 and 1,000,000 USDT");
                      return;
                    }
                    try {
                      const res = await api.futures.paperAddFunds({ amount: amt });
                      if (res?.error) { alert(res.error); return; }
                      refreshBots();
                    } catch (e) {
                      alert(`Add funds failed: ${e}`);
                    }
                  }}
                  className="text-[9px] text-emerald-400 hover:text-emerald-300 px-1.5 py-0.5 rounded border border-emerald-500/30 hover:bg-emerald-500/10"
                  title="Top up your paper wallet (paper mode only — not a real deposit)"
                >
                  + Funds
                </button>
              )}
              <button onClick={refreshBots} className="text-[9px] text-slate-500 hover:text-white">Refresh</button>
            </div>
          </div>
          <div className="overflow-y-auto flex-1 pr-0.5 space-y-1.5">
          {/* Main futures engine (started from Futures Paper/Live pages) */}
          {mainEngine && (
            <div className="p-2.5 rounded-lg bg-[#1e222d] border border-cyan-500/20 hover:border-cyan-500/40 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full animate-pulse bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,0.5)]" />
                  <span className="text-white text-[11px] font-bold">{mainEngine.strategy || 'Futures Engine'}</span>
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${
                    mainEngine.mode === 'live' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-indigo-500/20 text-indigo-400'
                  }`}>{mainEngine.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
                  <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-cyan-500/15 text-cyan-400">MAIN</span>
                </div>
                <button
                  onClick={async () => {
                    await api.futures.stop();
                    refreshBots();
                  }}
                  className="text-red-400 hover:text-red-300 text-[10px] font-medium px-2 py-0.5 rounded bg-red-500/10 border border-red-500/20"
                >
                  Stop
                </button>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-2 text-[10px]">
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Pairs</p>
                  <p className="text-white font-medium">{(mainEngine.pairs || []).join(', ') || '—'}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Leverage</p>
                  <p className="text-white font-medium">{mainEngine.leverage || 1}x</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Balance</p>
                  <p className="text-white font-medium">{(mainEngine.balance || 0).toFixed(1)}</p>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-1.5 text-[10px]">
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Trades</p>
                  <p className="text-white font-medium">{mainEngine.total_trades || 0}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Open</p>
                  <p className={`font-medium ${(mainEngine.open_trades || 0) > 0 ? 'text-emerald-400' : 'text-slate-400'}`}>{mainEngine.open_trades || 0}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">P&L</p>
                  <p className={`font-bold ${(mainEngine.realized_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(mainEngine.realized_pnl || 0) >= 0 ? '+' : ''}{(mainEngine.realized_pnl || 0).toFixed(2)}
                  </p>
                </div>
              </div>
              {mainEngine.last_action && (
                <div className="mt-2 p-1.5 rounded bg-[#131722] border border-white/[0.03]">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[9px] text-slate-500">Signal:</span>
                    <span className={`text-[9px] font-medium ${
                      mainEngine.last_action.toLowerCase().includes('long') || mainEngine.last_action.toLowerCase().includes('buy')
                        ? 'text-emerald-400'
                        : mainEngine.last_action.toLowerCase().includes('short') || mainEngine.last_action.toLowerCase().includes('sell')
                          ? 'text-red-400'
                          : 'text-slate-400'
                    }`}>{mainEngine.last_action}</span>
                  </div>
                </div>
              )}
            </div>
          )}
          {runningBots.filter(b => b.is_running).map(bot => (
            <div key={bot.id} onClick={() => setViewingBotId(bot.id)} className={`p-2.5 rounded-lg bg-[#1e222d] border cursor-pointer transition-colors ${
              bot.winding_down ? 'border-amber-500/20 hover:border-amber-500/40' : 'border-emerald-500/10 hover:border-emerald-500/30'
            }`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full animate-pulse ${
                    bot.winding_down ? 'bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.5)]' : 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.5)]'
                  }`} />
                  <span className="text-white text-[11px] font-bold">{bot.strategy_name}</span>
                  {bot.winding_down && (
                    <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-amber-500/20 text-amber-400">CLOSING</span>
                  )}
                  {bot.paused && !bot.winding_down && (
                    <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-purple-500/20 text-purple-400">PAUSED</span>
                  )}
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${
                    bot.mode === 'live' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-indigo-500/20 text-indigo-400'
                  }`}>{bot.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  {/* NICE-6 — Pause / Resume button. Hidden during wind-down
                      (the bot is already on its way out). Pausing blocks new
                      entries but keeps managing open positions. */}
                  {!bot.winding_down && (
                    <button
                      onClick={async (e) => {
                        e.stopPropagation();
                        if (bot.paused) await api.futures.bots.resume(bot.id);
                        else            await api.futures.bots.pause(bot.id);
                        refreshBots();
                      }}
                      className={`text-[10px] font-medium px-2 py-0.5 rounded border ${
                        bot.paused
                          ? 'text-emerald-400 hover:text-emerald-300 bg-emerald-500/10 border-emerald-500/20'
                          : 'text-purple-400 hover:text-purple-300 bg-purple-500/10 border-purple-500/20'
                      }`}
                      title={bot.paused
                        ? 'Resume — new entries re-enabled.'
                        : 'Pause — open positions keep managing (TP/SL/liq), but no new entries.'}
                    >
                      {bot.paused ? 'Resume' : 'Pause'}
                    </button>
                  )}
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      await api.futures.bots.stop(bot.id, bot.winding_down);
                      refreshBots();
                    }}
                    className="text-red-400 hover:text-red-300 text-[10px] font-medium px-2 py-0.5 rounded bg-red-500/10 border border-red-500/20"
                  >
                    {bot.winding_down ? 'Force Stop' : 'Stop'}
                  </button>
                </div>
              </div>

              {/* Bot stats grid */}
              <div className="grid grid-cols-3 gap-2 mt-2 text-[10px]">
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Pair</p>
                  <p className="text-white font-medium">{bot.pairs}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Leverage</p>
                  <p className="text-white font-medium">{bot.leverage}x</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Risk</p>
                  <p className="text-white font-medium">{bot.risk_pct || 5}%</p>
                </div>
              </div>

              {/* P&L + Positions */}
              <div className="grid grid-cols-3 gap-2 mt-1.5 text-[10px]">
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Trades</p>
                  <p className="text-white font-medium">{bot.total_trades || 0}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">Open</p>
                  <p className={`font-medium ${bot.open_positions > 0 ? 'text-emerald-400' : 'text-slate-400'}`}>{bot.open_positions || 0}</p>
                </div>
                <div className="text-center p-1.5 rounded bg-[#131722]">
                  <p className="text-slate-500">P&L</p>
                  <p className={`font-bold ${(bot.total_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(bot.total_pnl || 0) >= 0 ? '+' : ''}{(bot.total_pnl || 0).toFixed(2)}
                  </p>
                </div>
              </div>

              {/* Risk Guard cooldown badge */}
              {bot.guard_state === 'cooldown' && (
                <div className="mt-2 px-1.5 py-1 rounded bg-amber-500/10 border border-amber-500/30 text-[9px] text-amber-300 font-medium">
                  ⏸ Risk Guard cooldown{guardMinsLeft(bot.guard_cooldown_until) ? ` — ${guardMinsLeft(bot.guard_cooldown_until)}m left` : ''} (loss streak — new entries paused)
                </div>
              )}

              {/* Signal / Last action */}
              <div className="mt-2 p-1.5 rounded bg-[#131722] border border-white/[0.03]">
                <div className="flex items-center gap-1.5">
                  <span className="text-[9px] text-slate-500">Signal:</span>
                  {bot.last_action ? (
                    <span className={`text-[9px] font-medium ${
                      bot.last_action.toLowerCase().includes('long') || bot.last_action.toLowerCase().includes('buy')
                        ? 'text-emerald-400'
                        : bot.last_action.toLowerCase().includes('short') || bot.last_action.toLowerCase().includes('sell')
                          ? 'text-red-400'
                          : 'text-slate-400'
                    }`}>{bot.last_action}</span>
                  ) : (
                    <span className="text-[9px] text-slate-500 italic">Waiting for signal... ({bot.ticks || 0} ticks scanned)</span>
                  )}
                </div>
              </div>
            </div>
          ))}
          </div>
        </div>
      )}

      {/* Spacer — only when initial load is done, there are no active
          bots, AND the library is collapsed. Otherwise either Active Bots
          (flex-1), the loading skeleton (flex-1), or the expanded
          library (flex-1 below) fills the space. */}
      {initialLoaded && !hasActiveContent && libraryCollapsed && (
        <div className="flex-1 min-h-0" />
      )}

      {/* Stopped Bots (recent) — collapsed footer section */}
      {runningBots.filter(b => !b.is_running).length > 0 && (
        <div className="px-3 py-2 border-b border-white/[0.06] shrink-0">
          <button
            onClick={() => setRecentCollapsed(v => !v)}
            className="flex items-center justify-between w-full mb-1.5 group"
            aria-expanded={!recentCollapsed}
          >
            <p className="text-[10px] text-slate-500 font-medium group-hover:text-slate-300">
              Recent Bots ({runningBots.filter(b => !b.is_running).length})
            </p>
            <svg
              className={`w-3 h-3 text-slate-500 group-hover:text-slate-300 transition-transform ${recentCollapsed ? '-rotate-90' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {!recentCollapsed && runningBots.filter(b => !b.is_running).slice(0, 3).map(bot => (
            <div key={bot.id} onClick={() => setViewingBotId(bot.id)} className="p-2 rounded-lg bg-[#1e222d]/50 border border-white/[0.03] mb-1 cursor-pointer hover:border-white/[0.08] transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-500" />
                  <span className="text-slate-400 text-[11px] font-medium">{bot.strategy_name}</span>
                </div>
                <span className={`text-[10px] font-bold ${(bot.total_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {(bot.total_pnl || 0) >= 0 ? '+' : ''}{(bot.total_pnl || 0).toFixed(2)} USDT
                </span>
              </div>
              <div className="flex items-center gap-3 mt-0.5 text-[9px] text-slate-600">
                <span>{bot.pairs}</span>
                <span>{bot.leverage}x</span>
                <span>{bot.total_trades || 0} trades</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Strategy Library collapse header — lets the user hide the whole
          template browser when they just want to watch Active Bots. */}
      <div className="px-3 pt-2 pb-1 border-b border-white/[0.06]">
        <button
          onClick={() => setLibraryCollapsed(v => !v)}
          className="flex items-center justify-between w-full group"
          aria-expanded={!libraryCollapsed}
        >
          <p className="text-[10px] text-slate-500 font-medium group-hover:text-slate-300">
            Strategy Library
          </p>
          <svg
            className={`w-3 h-3 text-slate-500 group-hover:text-slate-300 transition-transform ${libraryCollapsed ? '-rotate-90' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      </div>

      {/* Category filter */}
      {!libraryCollapsed && (
      <div className="flex items-center gap-1 px-3 py-2 border-b border-white/[0.06] overflow-x-auto scrollbar-none">
        {categories.map(c => (
          <button
            key={c.key}
            onClick={() => setCategory(c.key)}
            className={`px-2.5 py-1 rounded text-[11px] font-medium whitespace-nowrap ${
              category === c.key ? 'text-white underline underline-offset-4 decoration-2' : 'text-slate-400 hover:text-white'
            }`}
          >
            {c.label}
          </button>
        ))}
        <span className="text-slate-500 text-xs ml-auto">&gt;</span>
      </div>
      )}

      {/* Section labels + Bot cards.
          flex-1 when there are no active bots (library fills the panel,
          which is the "first-time / nothing-running" experience).
          max-h capped when active bots exist so the library scroll area
          stays modest and doesn't squish the running-bots cards. */}
      {!libraryCollapsed && (
      <div className={`${hasActiveContent ? 'max-h-[40vh]' : 'flex-1'} overflow-y-auto px-3 py-2 space-y-1`}>
        {/* User's own strategies first */}
        {userStrategyCards.length > 0 && (category === 'all' || category === 'ai') && (
          <>
            <p className="text-[10px] text-emerald-400 font-medium pt-1 pb-1">My Strategies (Lead Trading)</p>
            {userStrategyCards.map((bot, i) => (
              <BotCard key={`user-${i}`} bot={bot} onClick={() => setSelectedBot(bot)} />
            ))}
          </>
        )}

        {/* Grid Strategy */}
        {(category === 'all' || category === 'grid') && (
          <>
            {category === 'all' && <p className="text-[10px] text-slate-500 font-medium pt-3 pb-1">Grid Strategy</p>}
            {allBots.filter(b => b.category === 'grid').map((bot, i) => (
              <BotCard key={`grid-${i}`} bot={bot} onClick={() => setSelectedBot(bot)} />
            ))}
          </>
        )}

        {/* AI-Powered */}
        {(category === 'all' || category === 'ai') && (
          <>
            {category === 'all' && <p className="text-[10px] text-slate-500 font-medium pt-3 pb-1">AI-Powered</p>}
            {BUILT_IN_BOTS.filter(b => b.category === 'ai').map((bot, i) => (
              <BotCard key={`ai-${i}`} bot={bot} onClick={() => setSelectedBot(bot)} />
            ))}
          </>
        )}

        {/* Cost-Averaging */}
        {(category === 'all' || category === 'dca') && (
          <>
            {category === 'all' && <p className="text-[10px] text-slate-500 font-medium pt-3 pb-1">Cost-Averaging</p>}
            {allBots.filter(b => b.category === 'dca').map((bot, i) => (
              <BotCard key={`dca-${i}`} bot={bot} onClick={() => setSelectedBot(bot)} />
            ))}
          </>
        )}
      </div>
      )}
    </div>
  );
}

function BotCard({ bot, onClick }: { bot: StrategyCard; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left p-3 rounded-lg border border-white/[0.04] hover:border-emerald-500/30 transition-colors group"
    >
      <div className="flex items-start gap-3">
        {/* Icon */}
        <div className="w-9 h-9 rounded-lg bg-slate-800 flex items-center justify-center text-base shrink-0 border border-white/[0.06]">
          {bot.icon === '10X' ? (
            <span className="text-[10px] font-bold text-white">10X</span>
          ) : (
            <span>{bot.icon}</span>
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold text-white">{bot.label}</span>
            {bot.isNew && (
              <span className="text-[9px] px-1.5 py-0.5 rounded bg-red-500 text-white font-bold leading-none">NEW</span>
            )}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            {bot.tags.map((tag, ti) => (
              <span
                key={ti}
                className={`text-[10px] ${
                  tag === 'Beginner' ? 'text-emerald-400' :
                  tag === 'Advanced' ? 'text-purple-400' :
                  tag === 'Bull Markets' ? 'text-emerald-400' :
                  tag === 'Bear Markets' ? 'text-red-400' :
                  tag === 'Volatile Markets' ? 'text-orange-400' :
                  'text-slate-400'
                }`}
              >
                {tag}
              </span>
            ))}
          </div>
          <p className="text-[11px] text-slate-500 mt-1 line-clamp-2">{bot.description}</p>
          <div className="flex items-center gap-3 mt-1.5 text-[10px] text-slate-500">
            <span className="flex items-center gap-1">
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" /></svg>
              {formatUsers(bot.users)}
            </span>
            <span className="flex items-center gap-1 text-emerald-400">
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>
              {bot.profitPct.toFixed(2)}%
            </span>
          </div>
        </div>
        {/* Arrow */}
        <div className="w-7 h-7 rounded-full bg-emerald-500/20 flex items-center justify-center shrink-0 mt-1 group-hover:bg-emerald-500/30">
          <span className="text-emerald-400 text-xs">›</span>
        </div>
      </div>
    </button>
  );
}

function BotCreateFlow({ bot, pair, mode, paperBalance, strategies, onBack, onCreated }: {
  bot: StrategyCard; pair: string; mode: 'paper' | 'live'; paperBalance?: number;
  strategies: any[];
  onBack: () => void; onCreated: () => void;
}) {
  const [viewTab, setViewTab] = useState<'leaderboard' | 'create'>('create');
  const [leverage, setLeverage] = useState(5);
  const [investment, setInvestment] = useState('');
  const [stoploss, setStoploss] = useState('');
  const [takeprofit, setTakeprofit] = useState('');
  const [drawdownTolerance, setDrawdownTolerance] = useState(50);
  const [maxPositionPct, setMaxPositionPct] = useState(5);
  // Trade-limits override — user-tunable values that previously could only
  // be set via API. UI was showing the validator's inferred defaults
  // (e.g. 4/day for intraday) as if they were hard caps. Now exposed
  // as overridable inputs; 0 / blank = use strategy default.
  const [maxTradesPerDay, setMaxTradesPerDay] = useState<number | ''>('');
  const [cooldownCandles, setCooldownCandles] = useState<number | ''>('');
  // Consecutive-loss adaptive cooldown guardrail
  const [guardEnabled,     setGuardEnabled]     = useState(true);
  const [guardMaxConsec,   setGuardMaxConsec]   = useState(5);
  const [guardCooldownMin, setGuardCooldownMin] = useState(60);
  // Region / session preset → maps to UTC hours sent as session_start_hr_utc /
  // session_end_hr_utc. Lets the user pick "NY", "London", "Tokyo", or "24/7"
  // instead of having to know the UTC hour ranges. PDF §6 lists NY as the
  // recommended institutional window for crypto.
  const [sessionRegion, setSessionRegion] = useState<'ny' | 'london' | 'tokyo' | 'all'>('ny');
  // Equal-highs / equal-lows clustering threshold — only relevant for SMC
  // strategies. 0.1% default per PDF §3 Step 2 ("abs(high1 - high2) < threshold").
  // Tight markets need smaller threshold; volatile markets bigger.
  const [equalPriceThresh, setEqualPriceThresh] = useState<number | ''>('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSlModal, setShowSlModal] = useState(false);
  const [showTpModal, setShowTpModal] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  // Structured live-guardrail rejection payload. When present we render
  // the actionable "Run Backtest Now →" CTA + confidence/missing-fields
  // breakdown instead of a plain string.
  const [blockedDetails, setBlockedDetails] = useState<{
    confidence_score?: number;
    live_permission?:  string;
    missing_fields?:   string[];
    conflicts?:        string[];
    has_recent_backtest?: boolean;
    has_paper_dwell?:     boolean;
    paper_closed_count?:  number;
    min_paper_trades?:    number;
    resolver_notes?:   string[];
  } | null>(null);
  const [success, setSuccess] = useState<{ botId: number; engineKey: string } | null>(null);

  // ── Advanced Risk Management (ARM) state ─────────────────────────────────
  // Mirrors the backtest ARM panel (see futures-backtest/page.tsx). When OFF,
  // the strategy's TP closes 100% of the position (legacy single-TP behaviour).
  // When ON: strategy's TP becomes TP2, TP1 = midpoint(entry, TP2), at TP1
  // partial-close N% + move SL to break-even, and optionally trail SL up to
  // TP1 once price crosses midpoint(TP1, TP2).
  //
  // NOTE: ARM is now fully wired end-to-end. The live/paper engine enforces
  // TP1 partial-close + break-even move + trail-to-TP1 on every position
  // (FuturesEngine._place_live_partial_close), so these settings affect LIVE,
  // PAPER and BACKTEST identically — not backtest-only.
  const [armEnabled,     setArmEnabled]     = useState(false);
  const [armTp1ClosePct, setArmTp1ClosePct] = useState(50);
  const [armBeMode,      setArmBeMode]      = useState<'leverage' | 'manual_pct' | 'entry'>('leverage');
  const [armBeBufferPct, setArmBeBufferPct] = useState(1.0);
  const [armTrailToTp1,  setArmTrailToTp1]  = useState(true);

  // ── Position model (Phase 9 — hedge support) ─────────────────────────────
  // "single" = TV-default stop-and-reverse (opposite signal closes the open
  // position and opens the new one; pair nets to one position).
  // "hedge"  = a LONG and a SHORT may coexist on the same pair — opposite
  // signals open the OTHER side instead of closing. The live/paper engine
  // honours this via FuturesEngine._position_mode (gated stop-and-reverse).
  const [positionMode, setPositionMode] = useState<'single' | 'hedge'>('single');
  // ── SL/TP source ─────────────────────────────────────────────────────────
  // false = use the strategy's structural SL/TP when it provides them, else
  // the slider %s (default). true = force the slider Stop-Loss/Take-Profit %s
  // for EVERY trade, ignoring structural levels — the live/paper equivalent of
  // the backtest's "From sliders below". Persisted + enforced by the engine.
  const [forceSlider, setForceSlider] = useState(false);
  // ── Paper-mode cost realism (paper only) ───────────────────────────────────
  // false (default) = frictionless paper fills. true = deduct simulated KuCoin
  // fees + slippage from paper P&L so paper tracks live. Ignored in live mode.
  const [paperSimCosts, setPaperSimCosts] = useState(false);
  // Per-strategy option controls (CHoCH exit / LDC dynamic-exit / ATR-stops /
  // bar-hold). Keyed by flag name; falls back to each flag's default when unset.
  const [strategyFlags, setStrategyFlags] = useState<Record<string, boolean | number>>({});

  // ── Strategy option toggles (manifest-driven) ──────────────────────────
  // The bot runs each strategy AS DESIGNED, so it shows ONLY the flags the
  // strategy actually declares. The Bar-hold timer therefore appears only for
  // strategies that define one (StrategyAsh = 60, LDC = 4) — it is NOT imposed
  // on strategies without a hold (Bollinger, SimpleTarget, …). The engine
  // already enforces a strategy's declared hold by default (start_futures
  // reads class_max_hold_candles); these toggles let you raise/disable it.
  // (Experimenting with a hold on any strategy stays a Futures-Backtest-only
  //  sandbox feature.)
  const botFlags: StratFlag[] = STRATEGY_FLAGS[bot.name] || [];
  // Build the strategy_flags payload from the strategy's own declared flags.
  const buildBotFlags = (): Record<string, boolean | number> | undefined => {
    const out: Record<string, boolean | number> = {};
    for (const f of botFlags) out[f.key] = strategyFlags[f.key] ?? f.default;
    return Object.keys(out).length ? out : undefined;
  };

  // ── Phase 5e: Strategy preview state ─────────────────────────────────────
  // Tracked here so we can disable the Create button in LIVE mode when the
  // strategy isn't live_eligible (matches the backend's hard guardrail in
  // POST /api/futures/bots so we fail fast in the UI).
  const [livePermission, setLivePermission] = useState<string>('blocked');
  const [confidenceScore, setConfidenceScore] = useState<number>(0);
  const [liveAllowed, setLiveAllowed] = useState<boolean>(false);
  // Selected execution timeframe — exposed so the preview + TF check re-run
  // when the user changes it on the form.
  const [executionTimeframe, setExecutionTimeframe] = useState<string>('15m');
  // UX#15 — TF mismatch warning from /api/strategy/{id}/tf-check.
  const [tfWarning, setTfWarning] = useState<string | null>(null);

  useEffect(() => {
    const sid = bot.id || strategies.find(s => s.name === bot.name)?.id;
    if (!sid) { setTfWarning(null); return; }
    let cancelled = false;
    api.futures.strategyTfCheck(sid, executionTimeframe)
      .then((d: any) => {
        if (!cancelled) setTfWarning(d?.warning || null);
      })
      .catch(() => { if (!cancelled) setTfWarning(null); });
    return () => { cancelled = true; };
  }, [bot, executionTimeframe, strategies]);
  const [backtestData, setBacktestData] = useState<number[]>([]);
  const [backtestError, setBacktestError] = useState('');
  const [currentPrice, setCurrentPrice] = useState(0);
  const [liveBalance, setLiveBalance] = useState<number | null>(null);
  const [leadConnected, setLeadConnected] = useState<boolean | null>(null);

  useEffect(() => {
    // Only fetch real KuCoin balance in LIVE mode. In paper mode we use
    // the virtual paperBalance prop (defaults to 1000 USDT) — fetching
    // the live balance here was leaking real-account values into the
    // paper-mode form, which is exactly the bug we just fixed.
    if (mode !== 'live') {
      setLeadConnected(null);   // 'live' state is irrelevant in paper mode
      setLiveBalance(null);
      return;
    }
    api.futures.leadTradingStatus().then(d => {
      setLeadConnected(d.connected);
      if (d.connected && d.balance) setLiveBalance(d.balance);
    }).catch(() => setLeadConnected(false));
  }, [mode]);

  // The "available balance" shown in the Investment form and used as
  // the base for the percentage buttons. In paper mode this is the
  // virtual paper engine balance (NEVER touches real KuCoin data).
  // In live mode it's the real Lead Trading available balance.
  const availableBalance: number = mode === 'paper'
    ? (paperBalance ?? 1000)
    : (liveBalance ?? 0);

  useEffect(() => {
    api.market.price(pair).then(d => {
      if (d.price) setCurrentPrice(parseFloat(d.price));
    }).catch(() => {});
  }, [pair]);

  useEffect(() => {
    const base = currentPrice || 1.2;
    const fallback = Array.from({ length: 50 }, (_, i) =>
      base + Math.sin(i / 5) * 0.15 + (i / 50) * 0.1 + (Math.random() - 0.5) * 0.02
    );
    setBacktestData(fallback);

    const stratId = bot.id || strategies.find(s => s.name === bot.name)?.id;
    if (!stratId) return;

    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);

    fetch(`/api/futures/backtest/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strategy_id: stratId, pairs: [pair], timeframe: executionTimeframe,
        timerange: '20240901-20241201', leverage, starting_balance: 1000,
      }),
      signal: controller.signal,
    })
      .then(res => res.ok ? res.json() : null)
      .then(r => {
        clearTimeout(timer);
        if (cancelled || !r) return;
        if (r.equity_curve?.length) setBacktestData(r.equity_curve);
        else if (r.error) setBacktestError(r.error);
      })
      .catch(() => { clearTimeout(timer); });

    return () => { cancelled = true; controller.abort(); };
  }, [bot, pair, leverage, strategies, currentPrice, executionTimeframe]);

  async function createBot() {
    setSubmitting(true);
    setError('');
    try {
      const stratId = bot.id || strategies.find(s => s.name === bot.name)?.id;
      const r = await api.futures.bots.create({
        strategy_id: stratId,
        strategy_name: bot.name,
        mode,
        pairs: [pair],
        // Execution timeframe selected by the user. Per the hybrid-engine
        // spec (PDF §5), this drives the entry trigger TF + risk TF and
        // upgrades the original strategy TF to a bias/setup filter when
        // appropriate. Backend reads this on bot create and passes it to
        // the strategy runner + MTF analyzer.
        timeframe: executionTimeframe,
        leverage,
        wallet: parseFloat(investment) || 1000,
        stoploss: stoploss ? -(parseFloat(stoploss) / 100) : -0.03,
        takeprofit: takeprofit ? parseFloat(takeprofit) / 100 : 0.015,
        drawdown_tolerance: drawdownTolerance,
        max_position_pct: maxPositionPct,
        // Position model — "single" (stop-and-reverse) or "hedge" (LONG +
        // SHORT coexist). Backend persists it on the StrategyInstance and the
        // live/paper engine honours it (gated stop-and-reverse).
        position_mode: positionMode,
        // SL/TP source — true forces the slider %s for every trade (ignores
        // the strategy's structural levels). Backend persists + enforces it.
        force_slider_sltp: forceSlider,
        // Paper-mode realism — true deducts simulated KuCoin fees + slippage
        // from paper P&L (paper only; ignored in live). Off by default.
        paper_sim_costs: mode === 'paper' ? paperSimCosts : false,
        // Per-strategy option toggles (CHoCH / LDC dynamic-exit / ATR-stops).
        // Persisted on the StrategyInstance and applied every signal scan, so
        // the bot behaves the same as the backtest with these toggles.
        ...(buildBotFlags()
            ? { strategy_flags: buildBotFlags() } : {}),
        // Advanced Risk Management — backend stores AND enforces these in the
        // live/paper engine (TP1 partial-close + BE-trail). UI matches backtest.
        arm_enabled:        armEnabled,
        arm_tp1_close_pct:  armTp1ClosePct,
        arm_be_mode:        armBeMode,
        arm_be_buffer_pct:  armBeBufferPct,
        arm_trail_to_tp1:   armTrailToTp1,
        // Consecutive-loss adaptive cooldown guardrail.
        guard_enabled:      guardEnabled,
        guard_max_consec:   guardMaxConsec,
        guard_cooldown_min: guardCooldownMin,
        // Trade-limits override (when blank, backend uses validator's
        // mode-based default per PDF §7 safe-defaults table).
        ...(maxTradesPerDay !== '' ? { max_trades_per_day: maxTradesPerDay } : {}),
        ...(cooldownCandles !== '' ? { cooldown_candles:   cooldownCandles } : {}),
        // Session/region preset → UTC hour range. Sent only when not "all".
        ...(sessionRegion === 'ny'      ? { session_start_hr_utc: 12, session_end_hr_utc: 21 } : {}),
        ...(sessionRegion === 'london'  ? { session_start_hr_utc:  7, session_end_hr_utc: 16 } : {}),
        ...(sessionRegion === 'tokyo'   ? { session_start_hr_utc:  0, session_end_hr_utc:  9 } : {}),
        ...(sessionRegion === 'all'     ? { session_start_hr_utc:  0, session_end_hr_utc: 23 } : {}),
        // Equal-price threshold (SMC strategies only; ignored by others).
        ...(equalPriceThresh !== '' ? { equal_price_thresh_pct: equalPriceThresh } : {}),
      });
      if (r?.error) {
        setError(r.error);
        // When the backend returns a structured live-guardrail rejection,
        // stash the details so we can render a richer error panel with a
        // one-click "Run Backtest Now" CTA.
        if (r.blocked_reason === 'live_guardrail') {
          setBlockedDetails({
            confidence_score:    r.confidence_score,
            live_permission:     r.live_permission,
            missing_fields:      r.missing_fields  || [],
            conflicts:           r.conflicts       || [],
            has_recent_backtest: r.has_recent_backtest,
            has_paper_dwell:     r.has_paper_dwell,
            paper_closed_count:  r.paper_closed_count,
            min_paper_trades:    r.min_paper_trades,
            resolver_notes:      r.resolver_notes  || [],
          });
        } else {
          setBlockedDetails(null);
        }
      } else {
        setSuccess({ botId: r.bot_id, engineKey: r.engine_key });
        setBlockedDetails(null);
        onCreated();
        // Auto-return to the strategy list after a brief confirmation, so
        // the new bot appears in Active Bots without the user having to
        // click "Back to Strategies". Fixes the "can't see activated
        // strategies" UX bug — Active Bots is rendered above the Create
        // flow and is hidden while BotCreateFlow holds the panel.
        setTimeout(() => onBack(), 2500);
      }
    } catch (e: any) {
      const msg = e?.message || String(e);
      if (msg.includes('HTTP 5')) setError('Server error — please try again');
      else if (msg.includes('HTTP 4')) setError('Request failed — check your settings');
      else setError(msg.length > 200 ? 'Failed to create bot — please try again' : msg);
    }
    setSubmitting(false);
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <span className="text-xs text-slate-400 font-medium">Place Order</span>
        <div className="ml-auto">
          <button className="text-slate-500 text-xs">⋮</button>
        </div>
      </div>

      {/* Bot name + back */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <button onClick={onBack} className="text-slate-400 hover:text-white text-sm">&lt;</button>
        <span className="text-sm font-bold text-white">{bot.label}</span>
        <button className="ml-auto text-slate-500 hover:text-white text-xs">?</button>
      </div>

      {/* Mode indicator — Lead Trading status in live mode, Paper Mode in paper. */}
      <div className={`flex items-center gap-2 px-3 py-1.5 text-[10px] border-b border-white/[0.06] ${
        mode === 'paper'
          ? 'bg-indigo-500/10'
          : leadConnected ? 'bg-emerald-500/10' : 'bg-amber-500/10'
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full ${
          mode === 'paper' ? 'bg-indigo-400'
            : leadConnected ? 'bg-emerald-400' : 'bg-amber-400'
        }`} />
        <span className={
          mode === 'paper' ? 'text-indigo-300 font-medium'
            : leadConnected ? 'text-emerald-400 font-medium' : 'text-amber-400 font-medium'
        }>
          {mode === 'paper'
            ? `Paper Mode — Simulated (${(paperBalance ?? 1000).toFixed(2)} USDT virtual)`
            : (leadConnected ? 'Lead Trading Connected' : 'Lead Trading: Not Connected')}
        </span>
        <span className="text-slate-500 ml-auto">
          {mode === 'live'
            ? (leadConnected ? 'Followers will copy this bot' : 'Configure API in Setup')
            : 'No real money — fully virtual'}
        </span>
      </div>

      {/* Leaderboard / Create tabs */}
      <div className="flex items-center border-b border-white/[0.06]">
        {(['leaderboard', 'create'] as const).map(t => (
          <button
            key={t}
            onClick={() => setViewTab(t)}
            className={`flex-1 py-2.5 text-xs font-medium capitalize ${
              viewTab === t
                ? 'text-white bg-white/[0.06] rounded-t'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            {t === 'leaderboard' ? 'Leaderboard' : 'Create'}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {viewTab === 'leaderboard' && (
          <LeaderboardView botName={bot.label} pair={pair} />
        )}

        {viewTab === 'create' && success && (
          <div className="px-3 py-6 flex flex-col items-center gap-4">
            <div className="w-14 h-14 rounded-full bg-emerald-500/20 flex items-center justify-center">
              <span className="text-emerald-400 text-2xl">&#10003;</span>
            </div>
            <div className="text-center">
              <p className="text-white font-bold text-sm">Bot Created Successfully</p>
              <p className="text-slate-400 text-xs mt-1">{bot.label} is now running on {pair}</p>
              <p className="text-slate-500 text-[10px] mt-0.5">{mode === 'live' ? 'Live Mode — Trades will appear in KuCoin Lead Trading' : 'Paper Mode — Simulated trades'}</p>
            </div>
            <div className="w-full p-3 rounded-lg bg-[#1e222d] border border-white/[0.06] space-y-2 text-[11px]">
              <div className="flex justify-between"><span className="text-slate-400">Strategy</span><span className="text-white">{bot.label}</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Pair</span><span className="text-white">{pair}</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Leverage</span><span className="text-white">{leverage}x</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Investment</span><span className="text-white">{parseFloat(investment) || 1000} USDT</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Risk/Trade</span><span className="text-white">{maxPositionPct}%</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Position model</span><span className={positionMode === 'hedge' ? 'text-purple-300' : 'text-sky-300'}>{positionMode === 'hedge' ? 'Hedge (long + short)' : 'Single (TV)'}</span></div>
              {armEnabled && (
                <div className="flex justify-between"><span className="text-slate-400">ARM</span><span className="text-purple-300">TP1 {armTp1ClosePct}% + BE trail</span></div>
              )}
              <div className="flex justify-between"><span className="text-slate-400">Mode</span><span className={mode === 'live' ? 'text-emerald-400' : 'text-indigo-400'}>{mode === 'live' ? 'Live (Lead Trading)' : 'Paper'}</span></div>
            </div>
            <p className="text-[10px] text-slate-500 -mt-1">Returning to Active Bots in a moment…</p>
            <div className="flex gap-2 w-full mt-1">
              <button
                onClick={onBack}
                className="flex-1 py-2.5 rounded-lg bg-emerald-500 text-white text-xs font-bold hover:bg-emerald-400 transition-colors"
              >
                View Active Bots →
              </button>
            </div>
          </div>
        )}

        {viewTab === 'create' && !success && (
          <div className="px-3 py-3 space-y-4">
            {/* ── Phase 5e — Decoded Strategy Preview ────────────────────
                Shows the engine's interpretation of the strategy: decoded
                rules grouped by role (bias filter / entry trigger / etc.),
                risk plan, trade limits, confidence score, and missing /
                inferred fields. When mode=live AND live_permission is not
                'live_eligible', the Create button below is auto-disabled
                so the user can't bypass the hard backend guardrail. */}
            {tfWarning && (
              <div className="text-[10px] text-amber-200 bg-amber-500/5 border border-amber-500/30 rounded px-2 py-1.5 leading-snug">
                {tfWarning}
              </div>
            )}
            {(bot.id || strategies.find(s => s.name === bot.name)?.id) && (
              <StrategyPreview
                strategyId={bot.id || (strategies.find(s => s.name === bot.name)?.id ?? null)}
                timeframe={executionTimeframe}
                mode={mode}
                onPermissionChange={(perm, score, ok) => {
                  setLivePermission(perm);
                  setConfidenceScore(score);
                  setLiveAllowed(ok);
                }}
              />
            )}

            {/* Execution Timeframe — drives Strategy Preview, backtest sample,
                live signal cadence, and (per hybrid-engine spec §5) which TF
                acts as the bias filter vs the entry trigger. Selecting 1m
                makes the bot a scalper, 1h makes it a swing bot, etc. The
                strategy itself stays the same — only the execution clock and
                the MTF role assignments change. */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-white">Execution timeframe</span>
                <span className="text-xs font-bold text-emerald-400">{executionTimeframe}</span>
              </div>
              <div className="flex gap-1.5">
                {['1m', '5m', '15m', '1h', '4h'].map(tf => (
                  <button
                    key={tf}
                    onClick={() => setExecutionTimeframe(tf)}
                    title={
                      tf === '1m' ? 'Scalp mode — entry trigger every minute'
                      : tf === '5m' ? 'Scalp / fast intraday — entry every 5 min'
                      : tf === '15m' ? 'Intraday — entry every 15 min'
                      : tf === '1h' ? 'Swing — entry every hour'
                      : 'Position trading — entry every 4 hours'
                    }
                    className={`flex-1 py-1.5 rounded text-[11px] font-medium transition-colors ${
                      executionTimeframe === tf
                        ? 'bg-sky-500 text-white'
                        : 'bg-[#1e222d] text-slate-400 hover:text-white border border-white/[0.06]'
                    }`}
                  >
                    {tf}
                  </button>
                ))}
              </div>
              {tfWarning && (
                <div className="mt-1 text-[10px] text-amber-300 leading-snug">
                  ⚠️ {tfWarning}
                </div>
              )}
            </div>

            {/* Backtest chart */}
            <div>
              <div className="flex items-center gap-1 mb-2">
                <span className="text-xs font-medium text-white">Backtest</span>
                <span className="text-slate-500 text-[10px]">ⓘ</span>
                {backtestError && <span className="text-amber-400 text-[10px] ml-1">{backtestError}</span>}
              </div>
              <BacktestChart data={backtestData} currentPrice={currentPrice} />
            </div>

            {/* Margin / Leverage */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-white">Leverage</span>
                <span className="text-xs font-bold text-emerald-400">{leverage}x</span>
              </div>
              <div className="flex gap-1.5">
                {[1, 2, 3, 5, 10, 15, 20].map(l => (
                  <button
                    key={l}
                    onClick={() => setLeverage(l)}
                    className={`flex-1 py-1.5 rounded text-[11px] font-medium transition-colors ${
                      leverage === l
                        ? 'bg-emerald-500 text-white'
                        : 'bg-[#1e222d] text-slate-400 hover:text-white border border-white/[0.06]'
                    }`}
                  >
                    {l}x
                  </button>
                ))}
              </div>
            </div>

            {/* Investment (Margin) */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-white">Investment (Margin)</span>
              </div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-slate-500">Available</span>
                <span className="text-[10px] text-emerald-400 font-medium">
                  {mode === 'paper'
                    ? `${availableBalance.toFixed(2)} USDT (Sim)`
                    : (liveBalance !== null ? `${liveBalance.toFixed(2)} USDT` : '— USDT')}
                  {' '}<span className="cursor-pointer">⊕</span>
                </span>
              </div>
              <div className="flex items-center bg-[#1e222d] rounded border border-white/[0.06]">
                <input
                  type="number"
                  value={investment}
                  onChange={e => setInvestment(e.target.value)}
                  placeholder="Min: 1"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <div className="flex items-center gap-1 pr-2">
                  <span className="text-xs text-slate-400">USDT</span>
                  <div className="flex flex-col">
                    <button className="text-slate-500 text-[8px] leading-none hover:text-white">▲</button>
                    <button className="text-slate-500 text-[8px] leading-none hover:text-white">▼</button>
                  </div>
                </div>
              </div>
              <div className="flex gap-1.5 mt-2">
                {['Min', '25%', '50%', '75%', '100%'].map(label => (
                  <button
                    key={label}
                    onClick={() => {
                      // Paper mode percentage buttons use the virtual paper
                      // balance, NOT the real KuCoin balance. Live mode
                      // uses the real live balance if connected.
                      const base = availableBalance > 0 ? availableBalance : 1000;
                      if (label === 'Min') setInvestment('1');
                      else setInvestment(String(Math.round(base * parseInt(label) / 100)));
                    }}
                    className="flex-1 py-1.5 rounded text-[10px] text-slate-400 bg-[#1e222d] border border-white/[0.06] hover:border-emerald-500/30 hover:text-white transition-colors"
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            {/* Wallet % Risk Control — always visible */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-bold text-white">Position Size</span>
                <span className="text-xs text-emerald-400 font-bold">{maxPositionPct}% of wallet</span>
              </div>
              <p className="text-[10px] text-slate-500 mb-2">
                Margin staked per trade — this is what actually sizes every position.
                Any "risk %" inside the strategy's own rules is a signal/SL parameter and does <b>not</b> change this.
              </p>
              <div className="flex gap-1.5">
                {[2, 5, 10, 15, 25].map(pct => (
                  <button
                    key={pct}
                    onClick={() => setMaxPositionPct(pct)}
                    className={`flex-1 py-1.5 rounded text-[11px] font-bold border transition-colors ${
                      maxPositionPct === pct
                        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40'
                        : 'text-slate-400 bg-[#1e222d] border-white/[0.06] hover:border-emerald-500/30'
                    }`}
                  >
                    {pct}%
                  </button>
                ))}
              </div>
              <div className="mt-2 p-2 rounded bg-[#1e222d] border border-white/[0.04]">
                <div className="flex justify-between text-[10px]">
                  <span className="text-slate-500">Max per trade</span>
                  <span className="text-white font-medium">{((parseFloat(investment) || availableBalance || 1000) * maxPositionPct / 100).toFixed(2)} USDT</span>
                </div>
                <div className="flex justify-between text-[10px] mt-0.5">
                  <span className="text-slate-500">With {leverage}x leverage</span>
                  <span className="text-emerald-400 font-medium">{((parseFloat(investment) || availableBalance || 1000) * maxPositionPct / 100 * leverage).toFixed(2)} USDT position</span>
                </div>
              </div>
            </div>

            {/* Risk Guard — consecutive-loss adaptive cooldown. Pauses NEW
                entries after a losing streak, then resumes (open positions stay
                managed). Stops the bot trading into a hostile market. */}
            <div className="rounded-lg border border-[#2a3a52] p-2.5">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={guardEnabled}
                  onChange={e => setGuardEnabled(e.target.checked)}
                  className="accent-amber-500 w-3.5 h-3.5"
                />
                <span className="text-xs font-bold text-white">🛡️ Risk Guard — loss-streak cooldown</span>
                <span className="text-[8px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-300 ml-auto">
                  {guardEnabled ? 'on' : 'off'}
                </span>
              </label>
              <p className="text-[10px] text-slate-500 mt-1 leading-snug">
                After <b className="text-amber-300">{guardMaxConsec}</b> losses in a row, pause new entries for{' '}
                <b className="text-amber-300">{guardCooldownMin}m</b>, then resume. Open trades keep their SL/TP.
              </p>
              {guardEnabled && (
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <label className="text-[10px] text-slate-400">
                    Losses → cooldown
                    <input type="number" min={2} max={20} value={guardMaxConsec}
                      onChange={e => setGuardMaxConsec(Math.max(2, Math.min(20, Number(e.target.value) || 5)))}
                      className="block mt-1 w-full px-2 py-1.5 rounded bg-[#0f1729] border border-[#2a3a52] text-xs text-slate-100" />
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Cooldown (minutes)
                    <input type="number" min={5} max={1440} value={guardCooldownMin}
                      onChange={e => setGuardCooldownMin(Math.max(5, Math.min(1440, Number(e.target.value) || 60)))}
                      className="block mt-1 w-full px-2 py-1.5 rounded bg-[#0f1729] border border-[#2a3a52] text-xs text-slate-100" />
                  </label>
                </div>
              )}
            </div>

            {/* Advanced Risk Management — port of the futures-backtest ARM panel.
                When OFF (default), strategy TP closes 100% of position.
                When ON: strategy TP becomes TP2, TP1 = midpoint(entry, TP2),
                partial-close at TP1 + trail SL → BE → TP1. */}
            <div className="rounded-lg border border-[#2a3a52] p-2.5">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={armEnabled}
                  onChange={e => setArmEnabled(e.target.checked)}
                  className="accent-purple-500 w-3.5 h-3.5"
                />
                <span className="text-xs font-bold text-white">🎯 Advanced Risk Management</span>
                <span
                  className="text-[8px] font-medium px-1.5 py-0.5 rounded-full bg-purple-500/20 text-purple-300 ml-auto"
                  title="When ON: TP1 = midpoint(entry, strategy_tp). At TP1 hit, close N% of position and move SL to break-even. If price reaches midpoint(TP1, TP2), trail SL up to TP1. When OFF: single TP at strategy's value (closes 100%)."
                >
                  {armEnabled ? 'partial TP + BE trail' : 'single TP'}
                </span>
              </label>
              <p className="text-[10px] text-slate-500 mt-1 leading-snug">
                {armEnabled
                  ? <>Strategy's TP becomes <b className="text-purple-300">TP2</b>. Books partial at midpoint, trails SL → BE → TP1.</>
                  : <>Strategy's TP closes 100% of the position.</>}
              </p>

              {armEnabled && (
                <div className="mt-3 space-y-3">
                  {/* TP1 Booking % */}
                  <div>
                    <div className="flex items-center justify-between text-[10px] mb-1">
                      <span className="text-slate-400">TP1 Booking: <b className="text-purple-300">{armTp1ClosePct}%</b></span>
                      <span className="text-slate-500">TP2 Booking: {100 - armTp1ClosePct}%</span>
                    </div>
                    <input
                      type="range" min={1} max={99} step={1}
                      value={armTp1ClosePct}
                      onChange={e => setArmTp1ClosePct(Number(e.target.value))}
                      className="w-full accent-purple-500"
                      title="% of position closed at TP1 (midpoint of entry → strategy_tp). Remainder closes at TP2 or trailed SL."
                    />
                  </div>

                  {/* Break-even mode */}
                  <div>
                    <label className="text-[10px] text-slate-400 mb-1 block">Break-even mode</label>
                    <div className="inline-flex rounded-md border border-[#2a3a52] overflow-hidden text-[10px] font-medium w-full">
                      <button
                        type="button"
                        onClick={() => setArmBeMode('leverage')}
                        className={`flex-1 px-2 py-1 ${armBeMode === 'leverage'
                          ? 'bg-purple-500/20 text-purple-300'
                          : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                        title={`BE = entry × (1 + leverage/1000). With ${leverage}x leverage → ${(leverage/10).toFixed(1)}% buffer.`}
                      >
                        Leverage-auto
                      </button>
                      <button
                        type="button"
                        onClick={() => setArmBeMode('manual_pct')}
                        className={`flex-1 px-2 py-1 ${armBeMode === 'manual_pct'
                          ? 'bg-purple-500/20 text-purple-300'
                          : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                        title="BE = entry × (1 + buffer%) — user-set buffer below."
                      >
                        Manual %
                      </button>
                      <button
                        type="button"
                        onClick={() => setArmBeMode('entry')}
                        className={`flex-1 px-2 py-1 ${armBeMode === 'entry'
                          ? 'bg-purple-500/20 text-purple-300'
                          : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                        title="BE = entry (no buffer — pure zero-loss line)."
                      >
                        At entry
                      </button>
                    </div>
                    {armBeMode === 'manual_pct' && (
                      <div className="mt-2 flex items-center gap-1.5">
                        <input
                          type="number"
                          min={0} max={10} step={0.1}
                          value={armBeBufferPct}
                          onChange={e => setArmBeBufferPct(Number(e.target.value))}
                          className="flex-1 bg-[#1e222d] border border-white/[0.06] rounded px-2 py-1 text-[11px] text-white outline-none"
                          placeholder="Buffer %"
                        />
                        <span className="text-[9px] text-slate-500">% above entry (long) / below (short)</span>
                      </div>
                    )}
                    {armBeMode === 'leverage' && (
                      <div className="text-[9px] text-slate-500 mt-1">
                        With {leverage}x: BE buffer = <b className="text-purple-300">{(leverage/10).toFixed(1)}%</b>
                      </div>
                    )}
                  </div>

                  {/* Trail SL → TP1 */}
                  <div>
                    <label className="text-[10px] text-slate-400 mb-1 block">Trail SL upgrade</label>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={armTrailToTp1}
                        onChange={e => setArmTrailToTp1(e.target.checked)}
                        className="accent-purple-500 w-3.5 h-3.5"
                      />
                      <span className="text-[11px] text-slate-200">Trail SL → TP1 after halfway to TP2</span>
                    </label>
                    <div className="text-[9px] text-slate-500 mt-1">
                      {armTrailToTp1
                        ? 'Once price reaches midpoint(TP1, TP2), SL moves up from BE to TP1.'
                        : 'SL stays at BE after TP1 (no further trailing).'}
                    </div>
                  </div>

                  {/* Notice — ARM is now enforced live + paper + backtest */}
                  <div className="text-[9px] text-emerald-300/80 bg-emerald-500/5 border border-emerald-500/20 rounded px-2 py-1.5">
                    ✓ Enforced on this bot in <b>live, paper and backtest</b>. At TP1 the engine books the partial close (reduce-only on live KuCoin) and moves SL to break-even automatically.
                  </div>
                </div>
              )}
            </div>

            {/* ── Position model: Single (TV) | Hedge (Phase 9) ──────────────
                Single = TV-default stop-and-reverse (opposite signal closes
                the open position and opens the new one — pair nets to one
                position). Hedge = a LONG and a SHORT may coexist on the same
                pair; opposite signals open the OTHER side instead of closing.
                The live/paper engine honours this (gated stop-and-reverse). */}
            <div className="rounded-lg border border-[#2a3a52] p-2.5">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-bold text-white">Position model</span>
                <span className={`text-[8px] font-medium px-1.5 py-0.5 rounded-full ${
                  positionMode === 'hedge' ? 'bg-purple-500/20 text-purple-300' : 'bg-sky-500/20 text-sky-300'}`}>
                  {positionMode === 'hedge' ? 'LONG + SHORT' : 'stop-and-reverse'}
                </span>
              </div>
              <div className="inline-flex rounded-md border border-[#2a3a52] overflow-hidden text-[11px] font-medium w-full">
                <button
                  type="button"
                  onClick={() => setPositionMode('single')}
                  className={`flex-1 px-2 py-1.5 ${positionMode === 'single'
                    ? 'bg-sky-500/20 text-sky-300'
                    : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  title="TV-default: one position per pair. An opposite signal closes the existing position AND opens the new one (stop-and-reverse)."
                >
                  Single (TV)
                </button>
                <button
                  type="button"
                  onClick={() => setPositionMode('hedge')}
                  className={`flex-1 px-2 py-1.5 border-l border-[#2a3a52] ${positionMode === 'hedge'
                    ? 'bg-purple-500/20 text-purple-300'
                    : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  title="A LONG and a SHORT can be open at the same time on the same pair. Opposite signals open the other side instead of closing. Each position runs to its own SL/TP/ARM. Best for mean-reversion strategies (Bollinger Bands)."
                >
                  Hedge (LONG + SHORT)
                </button>
              </div>
              <p className="text-[10px] text-slate-500 mt-2 leading-snug">
                {positionMode === 'hedge'
                  ? <>Opposite signals open a <b className="text-purple-300">new</b> position — the existing side is never force-closed. Max 1 long + 1 short per pair.</>
                  : <>An opposite signal <b className="text-sky-300">flips</b> the position (closes the old, opens the new). One position per pair.</>}
              </p>
            </div>

            {/* ── SL/TP source: From strategy | From sliders ─────────────────
                Matches the Futures Backtest "SL/TP source" toggle so the bot
                uses the same stops you tested. "From strategy" = use the
                strategy's structural SL/TP when it has them (SMC pivots, LDC
                ATR), else the slider %s. "From sliders" = force the slider
                Stop-Loss/Take-Profit %s for every trade. Sent as
                force_slider_sltp; the engine persists + enforces it. */}
            <div className="rounded-lg border border-[#2a3a52] p-2.5">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-bold text-white">SL / TP source</span>
                <span className={`text-[8px] font-medium px-1.5 py-0.5 rounded-full ${
                  forceSlider ? 'bg-amber-500/20 text-amber-300' : 'bg-emerald-500/20 text-emerald-300'}`}>
                  {forceSlider ? 'slider %' : 'strategy / slider'}
                </span>
              </div>
              <div className="inline-flex rounded-md border border-[#2a3a52] overflow-hidden text-[11px] font-medium w-full">
                <button
                  type="button"
                  onClick={() => setForceSlider(false)}
                  className={`flex-1 px-2 py-1.5 ${!forceSlider
                    ? 'bg-emerald-500/20 text-emerald-300'
                    : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  title="Use the strategy's own structural SL/TP when it provides them (SMC pivots, LDC ATR stops); fall back to your slider %s otherwise. Recommended for structural strategies."
                >
                  From strategy
                </button>
                <button
                  type="button"
                  onClick={() => setForceSlider(true)}
                  className={`flex-1 px-2 py-1.5 border-l border-[#2a3a52] ${forceSlider
                    ? 'bg-amber-500/20 text-amber-300'
                    : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  title="Force your slider Stop-Loss/Take-Profit %s for EVERY trade, ignoring the strategy's structural levels. Matches the backtest's 'From sliders below'."
                >
                  From sliders
                </button>
              </div>
              <p className="text-[10px] text-slate-500 mt-2 leading-snug">
                {forceSlider
                  ? <>Every trade uses your <b className="text-amber-300">slider SL/TP</b> ({stoploss || '3'}% / {takeprofit || '1.5'}%). The strategy's structural levels are ignored — keep this the SAME as your backtest.</>
                  : <>Uses the strategy's <b className="text-emerald-300">structural</b> SL/TP when available, else your slider %s. Non-structural strategies (Bollinger, SimpleTarget) always use the sliders.</>}
              </p>
            </div>

            {/* ── Paper-mode realism: simulate fees + slippage (paper only) ──
                Paper normally fills frictionlessly (like the backtest's pure
                mode), so its P&L is slightly optimistic vs live. Turn this ON
                to deduct simulated KuCoin VIP0 fees (~0.06% taker / 0.02%
                maker) + a small slippage from paper P&L, so paper ≈ live.
                Optional, OFF by default — never forced. Hidden in live mode
                (the exchange charges real fees there). */}
            {mode === 'paper' && (
              <label className="flex items-start gap-2 cursor-pointer rounded-lg border border-[#2a3a52] p-2.5">
                <input
                  type="checkbox"
                  checked={paperSimCosts}
                  onChange={e => setPaperSimCosts(e.target.checked)}
                  className="accent-indigo-500 mt-0.5"
                />
                <span className="text-[11px] leading-snug">
                  <span className="text-slate-200 font-medium">Simulate fees &amp; slippage</span>
                  <span className="ml-1 text-[8px] font-medium px-1.5 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300">paper realism</span>
                  <span className="text-slate-500"> — deduct simulated KuCoin fees (~0.06% taker / 0.02% maker) + slippage from paper P&amp;L so it tracks live. OFF = frictionless paper (matches a pure backtest). Doesn't affect live bots.</span>
                </span>
              </label>
            )}

            {/* ── Strategy options (per-strategy flag toggles) ──────────────
                Shown for EVERY strategy: a universal Bar-hold timer plus any
                per-strategy flags (StrategyAsh CHoCH exit; LDC dynamic-exit /
                ATR-stops / kernel). Sent via strategy_flags — applied live +
                persisted + enforced by the engine, identical to the backtest. */}
            {botFlags.length > 0 && (
              <div className="rounded-lg border border-violet-500/30 bg-violet-500/[0.05] p-3">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="text-xs font-bold text-violet-200">🎛 {bot.name} options</span>
                  <span className="text-[9px] text-slate-500">applied live — keep these the same as your backtest</span>
                </div>
                <div className="flex flex-col gap-1.5">
                  {botFlags.map(f => {
                    // number flag with a "disable" value → On/Off switch + stepper
                    if (f.type === 'number' && f.disableValue !== undefined) {
                      const cur = Number(strategyFlags[f.key] ?? f.default);
                      const off = cur === f.disableValue;
                      const onVal = Number(f.default) !== f.disableValue
                        ? Number(f.default)
                        : (f.onValue ?? Math.max(f.min ?? 1, 20));
                      return (
                        <div key={f.key} className="flex items-start gap-2">
                          <label className="flex items-center gap-1 cursor-pointer mt-0.5 shrink-0">
                            <input
                              type="checkbox"
                              checked={!off}
                              onChange={e => setStrategyFlags(prev => ({ ...prev, [f.key]: e.target.checked ? onVal : (f.disableValue as number) }))}
                              className="accent-violet-500"
                            />
                            <span className={`text-[10px] font-bold w-6 ${off ? 'text-amber-300' : 'text-emerald-300'}`}>{off ? 'OFF' : 'ON'}</span>
                          </label>
                          <input
                            type="number"
                            min={f.min} max={f.max} step={f.step ?? 1}
                            value={off ? onVal : cur}
                            disabled={off}
                            onChange={e => setStrategyFlags(prev => ({ ...prev, [f.key]: Math.max(f.min ?? 1, Math.min(f.max ?? 9999, Number(e.target.value) || (f.min ?? 1))) }))}
                            className={`w-14 px-2 py-1 rounded bg-[#0f1729] border border-violet-500/30 text-[11px] text-slate-100 ${off ? 'opacity-40' : ''}`}
                          />
                          <span className="text-[11px] leading-snug">
                            <span className="text-slate-200 font-medium">{f.label}</span>
                            {off && <span className="ml-1 text-[10px] text-amber-300">disabled</span>}
                            <span className="text-slate-500"> — {f.hint}</span>
                          </span>
                        </div>
                      );
                    }
                    // plain number stepper
                    if (f.type === 'number') {
                      return (
                        <div key={f.key} className="flex items-start gap-2">
                          <input
                            type="number"
                            min={f.min} max={f.max} step={f.step ?? 1}
                            value={Number(strategyFlags[f.key] ?? f.default)}
                            onChange={e => setStrategyFlags(prev => ({ ...prev, [f.key]: Math.max(f.min ?? -9999, Math.min(f.max ?? 9999, Number(e.target.value) || 0)) }))}
                            className="w-14 px-2 py-1 rounded bg-[#0f1729] border border-violet-500/30 text-[11px] text-slate-100"
                          />
                          <span className="text-[11px] leading-snug">
                            <span className="text-slate-200 font-medium">{f.label}</span>
                            <span className="text-slate-500"> — {f.hint}</span>
                          </span>
                        </div>
                      );
                    }
                    // boolean checkbox
                    return (
                      <label key={f.key} className="flex items-start gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={Boolean(strategyFlags[f.key] ?? f.default)}
                          onChange={e => setStrategyFlags(prev => ({ ...prev, [f.key]: e.target.checked }))}
                          className="accent-violet-500 mt-0.5"
                        />
                        <span className="text-[11px] leading-snug">
                          <span className="text-slate-200 font-medium">{f.label}</span>
                          <span className="text-slate-500"> — {f.hint}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Advanced Settings */}
            <div>
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="flex items-center gap-1 text-xs text-slate-400 hover:text-white"
              >
                Advanced Settings (Optional) <span className="text-[10px]">{showAdvanced ? '▴' : '▾'}</span>
              </button>

              {showAdvanced && (
                <div className="mt-3 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-slate-400">Drawdown Tolerance</span>
                    <span className="text-[11px] text-white">{drawdownTolerance}% &gt;</span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-slate-400">Stop-Loss</span>
                    <button
                      onClick={() => setShowSlModal(true)}
                      className="text-[11px] text-slate-300 hover:text-white"
                    >
                      {stoploss ? `${stoploss}%` : 'Configure >'}
                    </button>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-slate-400">Take-Profit</span>
                    <button
                      onClick={() => setShowTpModal(true)}
                      className="text-[11px] text-slate-300 hover:text-white"
                    >
                      {takeprofit ? `${takeprofit}%` : 'Configure >'}
                    </button>
                  </div>

                  {/* ── Trade-limits override ──────────────────────────
                      Previously the validator's mode-based default (e.g.
                      4/day for 15m intraday) was treated as a hard cap
                      with no UI override — users couldn't ask for more
                      or fewer trades than the inferred number. These
                      inputs let them tune freely; blank = strategy
                      default per PDF §7 safe-defaults table. */}
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-slate-400" title="Max number of trades the bot can open per UTC day. Blank = strategy default (5-10 scalp / 2-5 intraday / 1-3 swing per PDF §7).">
                      Max trades / day
                    </span>
                    <input
                      type="number" min={1} max={1000}
                      value={maxTradesPerDay}
                      onChange={e => setMaxTradesPerDay(e.target.value === '' ? '' : Math.max(1, Math.min(1000, Number(e.target.value))))}
                      placeholder="unlimited"
                      className="w-20 px-2 py-1 text-[11px] bg-[#1e222d] border border-white/[0.08] rounded text-white"
                    />
                  </div>

                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-slate-400" title="Bars to wait after a trade closes before opening another. Blank = strategy default.">
                      Cooldown (candles)
                    </span>
                    <input
                      type="number" min={0} max={50}
                      value={cooldownCandles}
                      onChange={e => setCooldownCandles(e.target.value === '' ? '' : Math.max(0, Math.min(50, Number(e.target.value))))}
                      placeholder="default"
                      className="w-16 px-2 py-1 text-[11px] bg-[#1e222d] border border-white/[0.08] rounded text-white"
                    />
                  </div>

                  {/* ── Session region selector (PDF §6) ───────────────
                      Picks the institutional trading window for crypto.
                      PDF recommends NY for crypto; London + Tokyo
                      provided for non-USD-pair experiments. "24/7" =
                      no session filter. Maps to UTC hour ranges. */}
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-slate-400" title="Restrict trades to a specific institutional session. NY (12-21 UTC) = PDF §6 default. Pick 24/7 to disable.">
                      Session
                    </span>
                    <select
                      value={sessionRegion}
                      onChange={e => setSessionRegion(e.target.value as any)}
                      className="px-2 py-1 text-[11px] bg-[#1e222d] border border-white/[0.08] rounded text-white"
                    >
                      <option value="ny">NY (12-21 UTC)</option>
                      <option value="london">London (7-16 UTC)</option>
                      <option value="tokyo">Tokyo (0-9 UTC)</option>
                      <option value="all">24/7 (no filter)</option>
                    </select>
                  </div>

                  {/* ── Equal-price threshold (SMC strategies only) ─────
                      Only consumed by SMC family strategies (SMCStrategy1
                      and similar). Other strategies ignore it. */}
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-slate-400" title="Two highs within this % of each other count as 'equal' (liquidity cluster). PDF §3 Step 2. SMC strategies only. Default 0.1%.">
                      Equal-price threshold
                    </span>
                    <div className="flex items-center gap-1">
                      <input
                        type="number" min={0.01} max={5} step={0.01}
                        value={equalPriceThresh}
                        onChange={e => setEqualPriceThresh(e.target.value === '' ? '' : Math.max(0.01, Math.min(5, Number(e.target.value))))}
                        placeholder="0.10"
                        className="w-16 px-2 py-1 text-[11px] bg-[#1e222d] border border-white/[0.08] rounded text-white"
                      />
                      <span className="text-[10px] text-slate-500">%</span>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Structured live-guardrail rejection panel.
                Triggered when POST /api/futures/bots returns
                blocked_reason === 'live_guardrail'. Shows the missing
                pieces (confidence, fields, backtest) and offers a one-
                click "Run Backtest Now" CTA that deep-links into
                /futures-backtest pre-filled with this strategy + pair + TF. */}
            {blockedDetails && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 space-y-2">
                <div className="flex items-center gap-2 text-red-300 text-xs font-bold">
                  🛑 Live trading blocked
                  {typeof blockedDetails.confidence_score === 'number' && (
                    <span className="ml-auto bg-red-500/20 rounded px-1.5 py-0.5 text-[10px] font-mono">
                      {blockedDetails.confidence_score}/100 · {blockedDetails.live_permission}
                    </span>
                  )}
                </div>

                {/* Per-cause line items so the user sees exactly what to fix. */}
                <ul className="text-[10px] text-red-200/90 space-y-0.5">
                  {blockedDetails.has_recent_backtest === false && (
                    <li>📊 <b>No recent backtest</b> — required within last 30 days for this strategy/pair/TF.</li>
                  )}
                  {blockedDetails.has_paper_dwell === false && (
                    <li>📝 <b>Insufficient paper-trade history</b> — {blockedDetails.paper_closed_count}/{blockedDetails.min_paper_trades} closed paper trades. Run as a Paper bot first.</li>
                  )}
                  {(blockedDetails.missing_fields || []).length > 0 && (
                    <li>⚠️ Missing fields: <span className="font-mono">{blockedDetails.missing_fields!.join(', ')}</span></li>
                  )}
                  {(blockedDetails.conflicts || []).length > 0 && (
                    <li className="text-red-300">⚠️ {blockedDetails.conflicts!.join(' · ')}</li>
                  )}
                  {typeof blockedDetails.confidence_score === 'number' && blockedDetails.confidence_score < 85 && (
                    <li>📉 Confidence {blockedDetails.confidence_score}/100 — needs ≥85 for live.</li>
                  )}
                </ul>

                {/* Resolver audit trail (collapsible). */}
                {(blockedDetails.resolver_notes || []).length > 0 && (
                  <details className="text-[10px]">
                    <summary className="text-red-300/70 cursor-pointer">Why this failed (resolver notes)</summary>
                    <ul className="mt-1 pl-3 space-y-0.5 text-red-200/70">
                      {blockedDetails.resolver_notes!.map((n, i) => <li key={i}>• {n}</li>)}
                    </ul>
                  </details>
                )}

                {/* Actionable CTAs. */}
                <div className="flex flex-wrap gap-2 pt-1">
                  {blockedDetails.has_recent_backtest === false && (
                    <a
                      href={`/futures-backtest?strategy_id=${
                        bot.id || strategies.find(s => s.name === bot.name)?.id || ''
                      }&pair=${encodeURIComponent(pair)}&tf=${encodeURIComponent(executionTimeframe)}`}
                      className="text-[11px] font-bold px-3 py-1.5 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 transition-colors"
                      title="Open the Futures Backtest tab pre-filled with this strategy, pair, and timeframe."
                    >
                      ▶ Run Backtest Now
                    </a>
                  )}
                  <span className="text-[10px] text-indigo-300/80 italic self-center">
                    Tip: switch the Paper/Live toggle (top-right of the terminal) to Paper to test this bot without the guardrail.
                  </span>
                </div>
              </div>
            )}

            {/* Plain error fallback for non-guardrail failures. */}
            {error && !blockedDetails && <p className="text-red-400 text-xs">{error}</p>}
          </div>
        )}
      </div>

      {/* Create button — Phase 5e: in LIVE mode, disabled when the
          backend guardrail would reject it anyway (live_permission ≠
          'live_eligible'). Paper mode bypasses the guard so users can
          experiment freely with incomplete strategies. */}
      {viewTab === 'create' && !success && (
        <div className="px-3 py-3 border-t border-white/[0.06]">
          {mode === 'live' && !liveAllowed && livePermission !== 'blocked' && (
            <div className="text-[10px] text-red-300 bg-red-500/5 border border-red-500/20 rounded px-2 py-1.5 mb-2 leading-snug">
              🛑 Live trading blocked by guardrail — confidence {confidenceScore}/100,
              permission <span className="font-mono">{livePermission}</span>. Use Paper mode
              or refine the strategy until confidence reaches 85.
            </div>
          )}
          <button
            onClick={createBot}
            disabled={submitting || (mode === 'live' && !liveAllowed)}
            className="w-full py-3 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            title={mode === 'live' && !liveAllowed
              ? `Live blocked: confidence ${confidenceScore}/100, permission ${livePermission}`
              : ''}
          >
            {submitting
              ? 'Creating...'
              : `Create ${mode === 'live' ? '(Live — Lead Trading)' : '(Paper)'}`}
          </button>
        </div>
      )}

      {showSlModal && (
        <ConfigModal
          title="Stop-Loss"
          value={stoploss}
          placeholder="1-99"
          suffix="%"
          description="When the loss reaches the set percentage, the bot will be automatically terminated."
          onConfirm={v => { setStoploss(v); setShowSlModal(false); }}
          onReset={() => { setStoploss(''); setShowSlModal(false); }}
          onClose={() => setShowSlModal(false)}
        />
      )}

      {showTpModal && (
        <ConfigModal
          title="Take-Profit"
          value={takeprofit}
          placeholder="1-10000"
          suffix="%"
          description="When the profit reaches the set percentage, the bot will be automatically terminated."
          onConfirm={v => { setTakeprofit(v); setShowTpModal(false); }}
          onReset={() => { setTakeprofit(''); setShowTpModal(false); }}
          onClose={() => setShowTpModal(false)}
        />
      )}
    </div>
  );
}

function BacktestChart({ data, currentPrice }: { data: number[]; currentPrice: number }) {
  if (data.length === 0) {
    return <div className="h-[140px] bg-[#1e222d] rounded-lg flex items-center justify-center text-slate-500 text-xs">Loading chart...</div>;
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const w = 300;
  const h = 140;
  const padding = 4;

  const points = data.map((val, i) => {
    const x = padding + (i / (data.length - 1)) * (w - padding * 2);
    const y = h - padding - ((val - min) / range) * (h - padding * 2);
    return `${x},${y}`;
  });

  const fillPoints = [...points, `${w - padding},${h}`, `${padding},${h}`];
  const lastVal = data[data.length - 1];
  const lastY = h - padding - ((lastVal - min) / range) * (h - padding * 2);

  return (
    <div className="relative bg-[#131722] rounded-lg overflow-hidden border border-white/[0.04]">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{ height: 140 }}>
        <defs>
          <linearGradient id="chartFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="rgba(16,185,129,0.3)" />
            <stop offset="100%" stopColor="rgba(16,185,129,0)" />
          </linearGradient>
        </defs>
        <polygon points={fillPoints.join(' ')} fill="url(#chartFill)" />
        <polyline points={points.join(' ')} fill="none" stroke="#10b981" strokeWidth="1.5" />
        {/* Current price line */}
        <line x1={padding} y1={lastY} x2={w - padding} y2={lastY} stroke="#10b981" strokeWidth="0.5" strokeDasharray="3,3" opacity="0.5" />
      </svg>
      {/* Price label */}
      <div
        className="absolute right-2 text-[10px] bg-emerald-600 text-white px-1.5 py-0.5 rounded"
        style={{ top: `${(lastY / h) * 100}%`, transform: 'translateY(-50%)' }}
      >
        {lastVal.toFixed(4)}
      </div>
      {/* Y axis labels */}
      <div className="absolute right-2 top-1 text-[9px] text-slate-500">{max.toFixed(4)}</div>
      <div className="absolute right-2 bottom-1 text-[9px] text-slate-500">{min.toFixed(4)}</div>
    </div>
  );
}

function ConfigModal({ title, value, placeholder, suffix, description, onConfirm, onReset, onClose }: {
  title: string; value: string; placeholder: string; suffix: string; description?: string;
  onConfirm: (val: string) => void; onReset: () => void; onClose: () => void;
}) {
  const [val, setVal] = useState(value);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-[#1a1e2e] rounded-xl border border-white/[0.08] p-6 w-[380px] max-w-[90vw] shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-lg font-bold text-white">{title}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
        </div>

        {description && (
          <p className="text-xs text-slate-400 mb-4">{description}</p>
        )}

        <div className="flex items-center bg-[#1e222d] rounded-lg border border-white/[0.06] mb-6">
          <input
            type="number"
            value={val}
            onChange={e => setVal(e.target.value)}
            placeholder={placeholder}
            className="flex-1 bg-transparent px-4 py-3 text-white outline-none"
          />
          <span className="text-slate-400 pr-3">{suffix}</span>
        </div>

        <div className="flex gap-3">
          <button
            onClick={onReset}
            className="flex-1 py-2.5 rounded-lg border border-white/[0.1] text-slate-300 text-sm font-medium hover:bg-white/[0.05]"
          >
            Reset
          </button>
          <button
            onClick={() => onConfirm(val)}
            className="flex-1 py-2.5 rounded-lg bg-white text-black text-sm font-bold hover:bg-slate-200"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}

function BotDetailView({ botId, onBack, onStop }: { botId: number; onBack: () => void; onStop: () => void }) {
  const [data, setData] = useState<any>(null);
  const [tab, setTab] = useState<'signals' | 'positions' | 'trades'>('signals');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = useCallback(() => {
    api.futures.bots.performance(botId)
      .then(d => { if (d && !d.error) { setData(d); setError(''); } else { setError(d?.error || 'Failed to load'); } setLoading(false); })
      .catch(e => { setError(String(e?.message || e)); setLoading(false); });
  }, [botId]);

  useEffect(() => { refresh(); }, [refresh]);
  // Visibility-aware: stop polling bot performance when the tab is hidden.
  useVisibleInterval(refresh, 5000);

  if (loading) {
    return <div className="flex items-center justify-center h-full text-slate-500 text-xs">Loading bot data...</div>;
  }

  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 px-4">
        <p className="text-red-400 text-xs text-center">Failed to load bot details</p>
        <p className="text-slate-500 text-[10px] text-center">{error.length > 100 ? 'Server error — backend may be redeploying' : error}</p>
        <div className="flex gap-2">
          <button onClick={onBack} className="px-3 py-1.5 rounded text-xs text-slate-300 border border-white/[0.1] hover:bg-white/[0.05]">Back</button>
          <button onClick={refresh} className="px-3 py-1.5 rounded text-xs text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/10">Retry</button>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2">
        <p className="text-slate-500 text-xs">No data available</p>
        <button onClick={onBack} className="px-3 py-1.5 rounded text-xs text-slate-300 border border-white/[0.1] hover:bg-white/[0.05]">Back</button>
      </div>
    );
  }

  const actionLog = data.action_log || [];
  const openPositions = data.open_positions_detail || [];
  // closedTrades: prefer the engine's in-memory list (most recent, full
  // detail), but fall back to the DB-fetched `trades` array when the
  // engine list is empty (engine restarted, lost in-memory history).
  // Previously was `data.closed_trades_detail || data.trades || []`
  // — but empty array `[]` is TRUTHY in JS, so the fallback never
  // triggered. User saw card "Trades: 13" but inner tab "Trades (0)"
  // because the engine's empty array was preferred over the DB list.
  const engineTrades = data.closed_trades_detail || [];
  const dbTrades = data.trades || [];
  const closedTrades = engineTrades.length > 0 ? engineTrades : dbTrades;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <button onClick={onBack} className="text-slate-400 hover:text-white text-sm">&lt;</button>
        <span className="text-sm font-bold text-white">{data.strategy_name}</span>
        <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ml-1 ${
          data.winding_down ? 'bg-amber-500/20 text-amber-400'
            : data.is_running ? 'bg-emerald-500/20 text-emerald-400'
            : 'bg-slate-500/20 text-slate-400'
        }`}>{data.winding_down ? 'CLOSING POSITIONS' : data.is_running ? 'RUNNING' : 'STOPPED'}</span>
        {data.is_running && (
          <button onClick={(e) => { e.stopPropagation(); onStop(); }}
            className="ml-auto text-red-400 hover:text-red-300 text-[10px] font-medium px-2 py-0.5 rounded bg-red-500/10 border border-red-500/20">
            Stop
          </button>
        )}
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-5 gap-1.5 px-3 py-2 border-b border-white/[0.06]">
        <div className="text-center p-1.5 rounded bg-[#131722]">
          <p className="text-[9px] text-slate-500">P&L</p>
          <p className={`text-[11px] font-bold ${(data.realized_pnl || data.total_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {(data.realized_pnl || data.total_pnl || 0) >= 0 ? '+' : ''}{(data.realized_pnl || data.total_pnl || 0).toFixed(2)}
          </p>
        </div>
        <div className="text-center p-1.5 rounded bg-[#131722]">
          <p className="text-[9px] text-slate-500">Win Rate</p>
          <p className="text-[11px] font-bold text-white">{data.win_rate || 0}%</p>
        </div>
        <div className="text-center p-1.5 rounded bg-[#131722]">
          <p className="text-[9px] text-slate-500">Trades</p>
          <p className="text-[11px] font-bold text-white">{data.total_trades || 0}</p>
        </div>
        <div className="text-center p-1.5 rounded bg-[#131722]">
          <p className="text-[9px] text-slate-500">Signals</p>
          <p className="text-[11px] font-bold text-amber-400">{data.signal_count || 0}</p>
        </div>
        <div className="text-center p-1.5 rounded bg-[#131722]">
          <p className="text-[9px] text-slate-500">Ticks</p>
          <p className="text-[11px] font-bold text-white">{data.ticks || 0}</p>
        </div>
      </div>

      {/* Config row — shows the bot's CURRENT runtime config so the user
          can verify at a glance what settings the bot was created with.
          Was missing timeframe + ARM status previously, which made it
          impossible to confirm whether the bot was actually on the TF /
          ARM combo the user picked at Create time. */}
      <div className="flex items-center flex-wrap gap-x-3 gap-y-1 px-3 py-1.5 border-b border-white/[0.06] text-[10px] text-slate-400">
        <span>{data.pairs}</span>
        {data.timeframe && (
          <span className="text-sky-300" title="Execution timeframe — drives entry trigger cadence and MTF role assignment">
            TF: {data.timeframe}
          </span>
        )}
        <span>{data.leverage}x</span>
        <span>Risk: {data.risk_pct || 5}%</span>
        <span className={data.mode === 'live' ? 'text-emerald-400' : 'text-indigo-400'}>{data.mode}</span>
        {data.arm_enabled && (
          <span className="text-purple-300"
                title={`ARM: TP1 ${data.arm_tp1_close_pct}% close, BE mode=${data.arm_be_mode}, trail-to-TP1=${data.arm_trail_to_tp1 ? 'on' : 'off'}`}>
            ARM (TP1 {data.arm_tp1_close_pct}%)
          </span>
        )}
      </div>

      {/* Tab selector */}
      <div className="flex items-center border-b border-white/[0.06]">
        {(['signals', 'positions', 'trades'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`flex-1 py-2 text-[11px] font-medium capitalize ${
              tab === t ? 'text-white bg-white/[0.06]' : 'text-slate-400 hover:text-white'
            }`}>
            {t === 'signals' ? `Events (${actionLog.length})` : t === 'positions' ? `Open (${openPositions.length})` : `Trades (${closedTrades.length})`}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto">
        {tab === 'signals' && (
          <div className="px-3 py-2 space-y-1">
            {/* Signal criteria from strategy */}
            {data.signal_criteria && data.signal_criteria.length > 0 && (
              <div className="p-2.5 rounded-lg bg-[#0d1117] border border-indigo-500/10 mb-2">
                <p className="text-[10px] text-indigo-400 font-bold mb-1.5">Signal Criteria</p>
                {data.signal_criteria.map((c: any, i: number) => (
                  <div key={i} className="mb-1 last:mb-0">
                    <span className={`text-[9px] font-bold px-1 py-0.5 rounded mr-1.5 ${
                      c.name === 'LONG' ? 'bg-emerald-500/15 text-emerald-400'
                        : c.name === 'SHORT' ? 'bg-red-500/15 text-red-400'
                        : c.name === 'Risk' ? 'bg-amber-500/15 text-amber-400'
                        : 'bg-indigo-500/15 text-indigo-400'
                    }`}>{c.name}</span>
                    <span className="text-[9px] text-slate-400">{(c.conditions || []).join(' + ')}</span>
                  </div>
                ))}
              </div>
            )}
            {actionLog.length === 0 && (
              <p className="text-slate-500 text-xs text-center py-4">Nothing logged yet — bot is scanning every 60s.</p>
            )}
            {actionLog.length > 0 && (data.signal_count || 0) === 0 && (
              <p className="text-amber-400/70 text-[10px] text-center pb-2 px-2 leading-snug">
                Bot is healthy ({data.ticks || 0} ticks scanned) but no entry signals have fired yet —
                SMC strategies are deliberately selective (3-15 trades per month on 15M).
                Events below are engine lifecycle (compile / pause / resume).
              </p>
            )}
            {[...actionLog].reverse().map((log: any, i: number) => (
              <div key={i} className="p-2 rounded bg-[#131722] border border-white/[0.03]">
                <div className="flex items-center gap-2">
                  <span className={`w-1.5 h-1.5 rounded-full ${
                    log.type === 'opened' ? (log.direction === 'long' ? 'bg-emerald-400' : 'bg-red-400')
                    : log.type === 'closed' ? 'bg-blue-400'
                    : 'bg-amber-400'
                  }`} />
                  <span className={`text-[10px] font-bold uppercase ${
                    log.type === 'opened' ? (log.direction === 'long' ? 'text-emerald-400' : 'text-red-400')
                    : log.type === 'closed' ? ((log.pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400')
                    : 'text-amber-400'
                  }`}>{log.type} {log.direction || ''}</span>
                  <span className="text-[9px] text-slate-500 ml-auto">{new Date(log.ts).toLocaleTimeString()}</span>
                </div>
                <p className="text-[10px] text-slate-400 mt-0.5">{log.detail}</p>
                {log.price && (
                  <div className="flex items-center gap-3 mt-1 text-[9px]">
                    <span className="text-slate-500">Price: <span className="text-white">{log.price.toFixed(2)}</span></span>
                    {log.sl && <span className="text-slate-500">SL: <span className="text-red-400">{log.sl.toFixed(2)}</span></span>}
                    {log.tp && <span className="text-slate-500">TP: <span className="text-emerald-400">{log.tp.toFixed(2)}</span></span>}
                    {log.pnl != null && <span className={`font-bold ${log.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>P&L: {log.pnl >= 0 ? '+' : ''}{log.pnl.toFixed(2)}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === 'positions' && (
          <div className="px-3 py-2 space-y-1.5">
            {openPositions.length === 0 && (
              <p className="text-slate-500 text-xs text-center py-4">No open positions</p>
            )}
            {openPositions.map((pos: any, i: number) => (
              <div key={i} className="p-2.5 rounded-lg bg-[#131722] border border-white/[0.04]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                      pos.direction === 'long' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'
                    }`}>{pos.direction.toUpperCase()}</span>
                    <span className="text-white text-[11px] font-medium">{pos.pair}</span>
                  </div>
                  <span className="text-[10px] text-slate-400">{pos.leverage}x</span>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-2 text-[10px]">
                  <div><span className="text-slate-500">Entry:</span> <span className="text-white">{pos.entry?.toFixed(2)}</span></div>
                  <div><span className="text-slate-500">Current:</span> <span className="text-white">{pos.current_price?.toFixed(2)}</span></div>
                  <div><span className="text-slate-500">SL:</span> <span className="text-red-400">{pos.sl?.toFixed(2)}</span></div>
                  <div><span className="text-slate-500">TP:</span> <span className="text-emerald-400">{pos.tp?.toFixed(2)}</span></div>
                  <div><span className="text-slate-500">Size:</span> <span className="text-white">{pos.size?.toFixed(2)} USDT</span></div>
                  <div><span className="text-slate-500">Liq:</span> <span className="text-amber-400">{pos.liquidation_price?.toFixed(2)}</span></div>
                </div>
                <div className="mt-2 pt-1.5 border-t border-white/[0.04] flex justify-between">
                  <span className="text-[10px] text-slate-500">Unrealized P&L</span>
                  <span className={`text-[11px] font-bold ${(pos.unrealized_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(pos.unrealized_pnl || 0) >= 0 ? '+' : ''}{(pos.unrealized_pnl || 0).toFixed(4)} USDT
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === 'trades' && (
          <div className="px-3 py-2 space-y-1">
            {closedTrades.length === 0 && (
              <p className="text-slate-500 text-xs text-center py-4">No trades yet</p>
            )}
            {closedTrades.map((t: any, i: number) => (
              <div key={i} className="p-2 rounded bg-[#131722] border border-white/[0.03]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className={`text-[9px] font-bold px-1 py-0.5 rounded ${
                      t.direction === 'long' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'
                    }`}>{(t.direction || '').toUpperCase()}</span>
                    <span className="text-white text-[10px] font-medium">{t.pair}</span>
                  </div>
                  <span className={`text-[10px] font-bold ${(t.pnl || t.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(t.pnl || t.profit_abs || 0) >= 0 ? '+' : ''}{(t.pnl || t.profit_abs || 0).toFixed(2)} USDT
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1 text-[9px] text-slate-500">
                  <span>Entry: {(t.entry || t.entry_price || 0).toFixed(2)}</span>
                  <span>Exit: {(t.exit || t.exit_price || 0).toFixed(2)}</span>
                  <span className={`${(t.pnl_pct || t.profit_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {(t.pnl_pct || t.profit_pct || 0) >= 0 ? '+' : ''}{(t.pnl_pct || t.profit_pct || 0).toFixed(1)}%
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-0.5 text-[9px] text-slate-600">
                  <span>{t.reason || t.exit_reason || ''}</span>
                  {(t.closed_at || t.exit_time) && <span>{new Date(t.closed_at || t.exit_time).toLocaleString()}</span>}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Refresh indicator */}
      <div className="px-3 py-1.5 border-t border-white/[0.06] text-center">
        <span className="text-[9px] text-slate-600">Auto-refreshing every 5s</span>
      </div>
    </div>
  );
}

function LeaderboardView({ botName, pair }: { botName: string; pair: string }) {
  const [tab, setTab] = useState<'24h' | 'profits' | 'rate'>('24h');
  const baseCoin = pair.split('/')[0];

  return (
    <div className="px-3 py-3">
      <div className="flex items-center gap-2 mb-3">
        {['24h Ranking', 'Profits', 'Profit Rate'].map((label, i) => {
          const key = ['24h', 'profits', 'rate'][i] as typeof tab;
          return (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-2.5 py-1 rounded text-[11px] font-medium ${
                tab === key ? 'text-white bg-white/[0.08]' : 'text-slate-400 hover:text-white'
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>

      <div className="space-y-2">
        {[
          { pair: `${baseCoin}USDT Perpetual`, leverage: '5x', profit: '+7.54%', yield24h: '+43.42%', runtime: '20d 14h', followers: 150 },
          { pair: 'KASUSDT Perpetual', leverage: '10x', profit: '+13.35%', yield24h: '+32.91%', runtime: '2d 0h', followers: 156 },
          { pair: 'ETHUSDT Perpetual', leverage: '3x', profit: '+5.21%', yield24h: '+18.63%', runtime: '15d 8h', followers: 89 },
        ].map((item, i) => (
          <div key={i} className="p-3 rounded-lg bg-[#1e222d] border border-white/[0.04]">
            <div className="flex items-center justify-between mb-2">
              <div>
                <span className="text-xs font-bold text-white">{item.pair}</span>
                <span className="ml-2 text-[10px] text-slate-400">{item.leverage}</span>
              </div>
              <button className="px-3 py-1 rounded-full bg-emerald-500 text-white text-[10px] font-bold hover:bg-emerald-400">
                Create
              </button>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[10px]">
              <div>
                <span className="text-slate-500">Profit Rate</span>
                <div className="text-emerald-400 font-bold">{item.profit}</div>
              </div>
              <div>
                <span className="text-slate-500">24h Yield</span>
                <div className="text-emerald-400">{item.yield24h}</div>
              </div>
              <div>
                <span className="text-slate-500">Runtime</span>
                <div className="text-slate-300">{item.runtime}</div>
              </div>
              <div>
                <span className="text-slate-500">Followers</span>
                <div className="text-slate-300">{item.followers}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
