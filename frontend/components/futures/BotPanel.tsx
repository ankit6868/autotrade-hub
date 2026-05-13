'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

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

  const refreshBots = useCallback(() => {
    api.futures.bots.list(mode).then(d => setRunningBots(d.bots || [])).catch(() => {});
    api.futures.status().then(d => setMainEngine(d?.running ? d : null)).catch(() => setMainEngine(null));
  }, [mode]);

  useEffect(() => {
    api.strategy.list().then(d => setStrategies(d.strategies || [])).catch(() => {});
    api.futures.leadTradingStatus().then(d => setLeadStatus(d)).catch(() => {});
    refreshBots();
    const t = setInterval(refreshBots, 5000);
    return () => clearInterval(t);
  }, [refreshBots]);

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
      {/* Lead Trading / Mode Status — always visible */}
      <div className={`flex items-center justify-between px-3 py-2 text-xs font-bold border-b ${
        leadStatus?.connected
          ? 'bg-emerald-500/20 border-emerald-500/30'
          : mode === 'live'
            ? 'bg-amber-500/20 border-amber-500/30'
            : 'bg-indigo-500/20 border-indigo-500/30'
      }`}>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full shrink-0 ${
            leadStatus?.connected
              ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]'
              : mode === 'live' ? 'bg-amber-400' : 'bg-indigo-400'
          }`} />
          <span className={
            leadStatus?.connected ? 'text-emerald-300'
              : mode === 'live' ? 'text-amber-300' : 'text-indigo-300'
          }>
            {leadStatus?.connected
              ? mode === 'paper'
                /* Paper mode: show paper engine balance, NOT the KuCoin 0-balance */
                ? `Lead Trading Connected • ${(paperBalance ?? 1000).toFixed(2)} USDT`
                : `Lead Trading Connected • ${(leadStatus.balance ?? 0).toFixed(2)} USDT`
              : mode === 'live'
                ? 'Lead Trading: Not Connected'
                : `Paper Mode • ${(paperBalance ?? 1000).toFixed(2)} USDT`}
          </span>
        </div>
        {mode === 'paper' && (
          <span className="text-[10px] text-indigo-300 font-medium">Paper Mode</span>
        )}
      </div>

      {/* Running Bots — scrollable so 6+ bots don't overflow */}
      {(runningBots.filter(b => b.is_running).length > 0 || mainEngine) && (
        <div className="px-3 py-2 border-b border-white/[0.06] flex flex-col max-h-[320px]">
          <div className="flex items-center justify-between mb-1.5 shrink-0">
            <p className="text-[10px] text-emerald-400 font-bold">Active Bots ({runningBots.filter(b => b.is_running).length + (mainEngine ? 1 : 0)})</p>
            <button onClick={refreshBots} className="text-[9px] text-slate-500 hover:text-white">Refresh</button>
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
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${
                    bot.mode === 'live' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-indigo-500/20 text-indigo-400'
                  }`}>{bot.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
                </div>
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

      {/* Stopped Bots (recent) */}
      {runningBots.filter(b => !b.is_running).length > 0 && (
        <div className="px-3 py-2 border-b border-white/[0.06]">
          <p className="text-[10px] text-slate-500 font-medium mb-1.5">Recent Bots</p>
          {runningBots.filter(b => !b.is_running).slice(0, 3).map(bot => (
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

      {/* Category filter */}
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

      {/* Section labels + Bot cards */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
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

function BotCreateFlow({ bot, pair, mode, strategies, onBack, onCreated }: {
  bot: StrategyCard; pair: string; mode: 'paper' | 'live'; strategies: any[];
  onBack: () => void; onCreated: () => void;
}) {
  const [viewTab, setViewTab] = useState<'leaderboard' | 'create'>('create');
  const [leverage, setLeverage] = useState(5);
  const [investment, setInvestment] = useState('');
  const [stoploss, setStoploss] = useState('');
  const [takeprofit, setTakeprofit] = useState('');
  const [drawdownTolerance, setDrawdownTolerance] = useState(50);
  const [maxPositionPct, setMaxPositionPct] = useState(5);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSlModal, setShowSlModal] = useState(false);
  const [showTpModal, setShowTpModal] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState<{ botId: number; engineKey: string } | null>(null);
  const [backtestData, setBacktestData] = useState<number[]>([]);
  const [backtestError, setBacktestError] = useState('');
  const [currentPrice, setCurrentPrice] = useState(0);
  const [liveBalance, setLiveBalance] = useState<number | null>(null);
  const [leadConnected, setLeadConnected] = useState<boolean | null>(null);

  useEffect(() => {
    api.futures.leadTradingStatus().then(d => {
      setLeadConnected(d.connected);
      if (d.connected && d.balance) setLiveBalance(d.balance);
    }).catch(() => setLeadConnected(false));
  }, []);

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
        strategy_id: stratId, pairs: [pair], timeframe: '15m',
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
  }, [bot, pair, leverage, strategies, currentPrice]);

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
        leverage,
        wallet: parseFloat(investment) || 1000,
        stoploss: stoploss ? -(parseFloat(stoploss) / 100) : -0.03,
        takeprofit: takeprofit ? parseFloat(takeprofit) / 100 : 0.015,
        drawdown_tolerance: drawdownTolerance,
        max_position_pct: maxPositionPct,
      });
      if (r?.error) {
        setError(r.error);
      } else {
        setSuccess({ botId: r.bot_id, engineKey: r.engine_key });
        onCreated();
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

      {/* Lead Trading indicator — always visible */}
      <div className={`flex items-center gap-2 px-3 py-1.5 text-[10px] border-b border-white/[0.06] ${
        leadConnected ? 'bg-emerald-500/10' : 'bg-amber-500/10'
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full ${leadConnected ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        <span className={leadConnected ? 'text-emerald-400 font-medium' : 'text-amber-400 font-medium'}>
          {leadConnected ? 'Lead Trading Connected' : 'Lead Trading: Not Connected'}
        </span>
        <span className="text-slate-500 ml-auto">
          {mode === 'live'
            ? (leadConnected ? 'Followers will copy this bot' : 'Configure API in Setup')
            : 'Paper Mode — Simulated'}
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
              <div className="flex justify-between"><span className="text-slate-400">Mode</span><span className={mode === 'live' ? 'text-emerald-400' : 'text-indigo-400'}>{mode === 'live' ? 'Live (Lead Trading)' : 'Paper'}</span></div>
            </div>
            <div className="flex gap-2 w-full mt-2">
              <button onClick={onBack} className="flex-1 py-2.5 rounded-lg border border-white/[0.1] text-slate-300 text-xs font-medium hover:bg-white/[0.05]">
                Back to Strategies
              </button>
            </div>
          </div>
        )}

        {viewTab === 'create' && !success && (
          <div className="px-3 py-3 space-y-4">
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
                  {liveBalance !== null ? `${liveBalance.toFixed(2)} USDT` : mode === 'paper' ? '1,000 USDT (Sim)' : '— USDT'}
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
                      const base = liveBalance || 1000;
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
                <span className="text-xs font-bold text-white">Risk per Trade</span>
                <span className="text-xs text-emerald-400 font-bold">{maxPositionPct}% of wallet</span>
              </div>
              <p className="text-[10px] text-slate-500 mb-2">How much of your wallet balance each trade will use</p>
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
                  <span className="text-white font-medium">{((parseFloat(investment) || (liveBalance || 1000)) * maxPositionPct / 100).toFixed(2)} USDT</span>
                </div>
                <div className="flex justify-between text-[10px] mt-0.5">
                  <span className="text-slate-500">With {leverage}x leverage</span>
                  <span className="text-emerald-400 font-medium">{((parseFloat(investment) || (liveBalance || 1000)) * maxPositionPct / 100 * leverage).toFixed(2)} USDT position</span>
                </div>
              </div>
            </div>

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
                </div>
              )}
            </div>

            {error && <p className="text-red-400 text-xs">{error}</p>}
          </div>
        )}
      </div>

      {/* Create button */}
      {viewTab === 'create' && !success && (
        <div className="px-3 py-3 border-t border-white/[0.06]">
          <button
            onClick={createBot}
            disabled={submitting}
            className="w-full py-3 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50 transition-colors"
          >
            {submitting ? 'Creating...' : `Create ${mode === 'live' ? '(Live — Lead Trading)' : '(Paper)'}`}
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

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

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
  const closedTrades = data.closed_trades_detail || data.trades || [];

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

      {/* Config row */}
      <div className="flex items-center gap-3 px-3 py-1.5 border-b border-white/[0.06] text-[10px] text-slate-400">
        <span>{data.pairs}</span>
        <span>{data.leverage}x</span>
        <span>Risk: {data.risk_pct || 5}%</span>
        <span className={data.mode === 'live' ? 'text-emerald-400' : 'text-indigo-400'}>{data.mode}</span>
      </div>

      {/* Tab selector */}
      <div className="flex items-center border-b border-white/[0.06]">
        {(['signals', 'positions', 'trades'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`flex-1 py-2 text-[11px] font-medium capitalize ${
              tab === t ? 'text-white bg-white/[0.06]' : 'text-slate-400 hover:text-white'
            }`}>
            {t === 'signals' ? `Signals (${actionLog.length})` : t === 'positions' ? `Open (${openPositions.length})` : `Trades (${closedTrades.length})`}
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
              <p className="text-slate-500 text-xs text-center py-4">No signals yet — bot is scanning...</p>
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
