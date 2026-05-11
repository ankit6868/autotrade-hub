'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface Props {
  pair: string;
  mode: 'paper' | 'live';
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

export default function BotPanel({ pair, mode, onBotCreated }: Props) {
  const [category, setCategory] = useState<Category>('all');
  const [strategies, setStrategies] = useState<any[]>([]);
  const [selectedBot, setSelectedBot] = useState<StrategyCard | null>(null);
  const [leadStatus, setLeadStatus] = useState<{ connected: boolean } | null>(null);
  const [runningBots, setRunningBots] = useState<any[]>([]);

  useEffect(() => {
    api.strategy.list().then(d => setStrategies(d.strategies || [])).catch(() => {});
    api.futures.leadTradingStatus().then(d => setLeadStatus(d)).catch(() => {});
    api.futures.bots.list().then(d => setRunningBots(d.bots || [])).catch(() => {});
  }, []);

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

  if (selectedBot) {
    return (
      <BotCreateFlow
        bot={selectedBot}
        pair={pair}
        mode={mode}
        strategies={strategies}
        onBack={() => setSelectedBot(null)}
        onCreated={onBotCreated}
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
        mode === 'live'
          ? (leadStatus?.connected
              ? 'bg-emerald-500/20 border-emerald-500/30'
              : 'bg-amber-500/20 border-amber-500/30')
          : 'bg-indigo-500/20 border-indigo-500/30'
      }`}>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full shrink-0 ${
            mode === 'live'
              ? (leadStatus?.connected ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.6)]' : 'bg-amber-400')
              : 'bg-indigo-400'
          }`} />
          <span className={
            mode === 'live'
              ? (leadStatus?.connected ? 'text-emerald-300' : 'text-amber-300')
              : 'text-indigo-300'
          }>
            {mode === 'live'
              ? (leadStatus?.connected ? 'Lead Trading API Connected' : 'Lead Trading: Configure in Setup')
              : 'Paper Mode — Simulated Funds'}
          </span>
        </div>
      </div>

      {/* Running Bots */}
      {runningBots.filter(b => b.is_running).length > 0 && (
        <div className="px-3 py-2 border-b border-white/[0.06]">
          <p className="text-[10px] text-slate-500 font-medium mb-1.5">Active Bots ({runningBots.filter(b => b.is_running).length})</p>
          {runningBots.filter(b => b.is_running).map(bot => (
            <div key={bot.id} className="p-2 rounded-lg bg-[#1e222d] border border-white/[0.04] mb-1.5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  <span className="text-white text-[11px] font-medium">{bot.strategy_name}</span>
                </div>
                <button
                  onClick={async () => {
                    await api.futures.bots.stop(bot.id);
                    setRunningBots(prev => prev.map(b => b.id === bot.id ? { ...b, is_running: false } : b));
                  }}
                  className="text-red-400 hover:text-red-300 text-[10px] font-medium px-2 py-0.5 rounded bg-red-500/10 border border-red-500/20"
                >
                  Stop
                </button>
              </div>
              <div className="flex items-center gap-3 mt-1 text-[10px] text-slate-500">
                <span>{bot.pairs}</span>
                <span>{bot.leverage}x</span>
                <span>{bot.mode}</span>
                {bot.open_positions > 0 && <span className="text-emerald-400">{bot.open_positions} open</span>}
                {bot.ticks > 0 && <span>{bot.ticks} ticks</span>}
              </div>
              {bot.last_action && (
                <p className="text-[9px] text-slate-600 mt-1 truncate">{bot.last_action}</p>
              )}
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
  const [backtestData, setBacktestData] = useState<number[]>([]);
  const [currentPrice, setCurrentPrice] = useState(0);
  const [liveBalance, setLiveBalance] = useState<number | null>(null);

  useEffect(() => {
    if (mode === 'live') {
      api.futures.leadTradingStatus().then(d => {
        if (d.connected && d.balance) setLiveBalance(d.balance);
      }).catch(() => {});
    }
  }, [mode]);

  useEffect(() => {
    api.market.price(pair).then(d => {
      if (d.price) setCurrentPrice(parseFloat(d.price));
    }).catch(() => {});
  }, [pair]);

  useEffect(() => {
    const stratId = bot.id || strategies.find(s => s.name === bot.name)?.id;
    if (!stratId) {
      const fakeData = Array.from({ length: 50 }, (_, i) => {
        const base = currentPrice || 1.2;
        return base + Math.sin(i / 5) * 0.15 + (i / 50) * 0.1 + (Math.random() - 0.5) * 0.02;
      });
      setBacktestData(fakeData);
      return;
    }
    api.futures.backtest.run({
      strategy_id: stratId,
      pairs: [pair],
      timeframe: '15m',
      timerange: '20240901-20241201',
      leverage,
      starting_balance: 1000,
    }).then(r => {
      setBacktestData(r?.equity_curve || []);
    }).catch(() => {});
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
      if (r.error) setError(r.error);
      else {
        onCreated();
        onBack();
      }
    } catch (e) {
      setError(String(e));
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

      {/* Lead Trading indicator */}
      {mode === 'live' && (
        <div className="flex items-center gap-2 px-3 py-1.5 text-[10px] bg-emerald-500/10 border-b border-white/[0.06]">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          <span className="text-emerald-400 font-medium">Lead Trading Mode</span>
          <span className="text-slate-500 ml-auto">Followers will copy this bot</span>
        </div>
      )}

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

        {viewTab === 'create' && (
          <div className="px-3 py-3 space-y-4">
            {/* Backtest chart */}
            <div>
              <div className="flex items-center gap-1 mb-2">
                <span className="text-xs font-medium text-white">Backtest</span>
                <span className="text-slate-500 text-[10px]">ⓘ</span>
              </div>
              <BacktestChart data={backtestData} currentPrice={currentPrice} />
            </div>

            {/* Margin / Leverage */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400">Margin</span>
              <select
                value={leverage}
                onChange={e => setLeverage(parseInt(e.target.value))}
                className="bg-[#1e222d] border border-white/[0.06] rounded px-2 py-1 text-xs text-white outline-none cursor-pointer"
              >
                {[1, 2, 3, 5, 10, 20, 50, 75].map(l => (
                  <option key={l} value={l}>{l}x</option>
                ))}
              </select>
            </div>

            {/* Investment (Margin) */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-white">Investment (Margin)</span>
              </div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-slate-500">Available</span>
                <span className="text-[10px] text-emerald-400 font-medium">
                  {mode === 'live' && liveBalance !== null ? `${liveBalance.toFixed(2)} USDT` : '0 USDT'}
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
                      if (label === 'Min') setInvestment('1');
                      else setInvestment(String(Math.round(1000 * parseInt(label) / 100)));
                    }}
                    className="flex-1 py-1.5 rounded text-[10px] text-slate-400 bg-[#1e222d] border border-white/[0.06] hover:border-emerald-500/30 hover:text-white transition-colors"
                  >
                    {label}
                  </button>
                ))}
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
                  {/* Wallet % risk per trade */}
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] text-slate-400">Risk per Trade (% of wallet)</span>
                      <span className="text-[11px] text-white font-medium">{maxPositionPct}%</span>
                    </div>
                    <div className="flex gap-1">
                      {[2, 5, 10, 15, 25].map(pct => (
                        <button
                          key={pct}
                          onClick={() => setMaxPositionPct(pct)}
                          className={`flex-1 py-1 rounded text-[10px] font-medium border transition-colors ${
                            maxPositionPct === pct
                              ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40'
                              : 'text-slate-400 bg-[#1e222d] border-white/[0.06] hover:border-emerald-500/30'
                          }`}
                        >
                          {pct}%
                        </button>
                      ))}
                    </div>
                  </div>

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
      {viewTab === 'create' && (
        <div className="px-3 py-3 border-t border-white/[0.06]">
          <button
            onClick={createBot}
            disabled={submitting}
            className="w-full py-3 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50 transition-colors"
          >
            {submitting ? 'Creating...' : 'Create'}
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
