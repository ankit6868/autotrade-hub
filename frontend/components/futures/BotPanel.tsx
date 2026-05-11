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
  users?: number;
  profitPct?: number;
}

const BUILT_IN_BOTS: StrategyCard[] = [
  { id: null, name: 'SimpleTargetStrategy', label: 'AI Futures Trend', description: 'Automatically captures market trends, optimizing profits during uptrends or downtrends.', category: 'ai', tags: ['Beginner', 'Bull Markets'] },
  { id: null, name: 'MissCandleLongStrategy', label: 'DualFutures AI', description: 'Profit from long and short positions, perfect for volatile markets.', category: 'ai', tags: ['Beginner', 'Volatile Markets'] },
  { id: null, name: 'MacdCrossoverStrategy', label: 'Futures Martingale', description: 'For both long and short positions. Open positions in batches, sell for profit.', category: 'grid', tags: ['Advanced', 'Volatile Markets'] },
  { id: null, name: 'RsiBollingerStrategy', label: 'Smart Rebalance', description: 'An investment portfolio that spreads risks in the long-term.', category: 'dca', tags: ['Bull Markets'] },
  { id: null, name: 'DcaAccumulationStrategy', label: 'DCA', description: 'Make profits from regular investment.', category: 'dca', tags: ['Bull Markets'] },
  { id: null, name: 'EmaScalpingStrategy', label: 'Spot Martingale', description: 'Kill volatility by buying in stages and selling all at once.', category: 'grid', tags: ['Bull Markets'] },
];

export default function BotPanel({ pair, mode, onBotCreated }: Props) {
  const [category, setCategory] = useState<Category>('all');
  const [strategies, setStrategies] = useState<any[]>([]);
  const [selectedBot, setSelectedBot] = useState<StrategyCard | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    api.strategy.list().then(d => setStrategies(d.strategies || [])).catch(() => {});
  }, []);

  const allBots: StrategyCard[] = [
    ...BUILT_IN_BOTS,
    ...strategies.filter(s => !BUILT_IN_BOTS.find(b => b.name === s.name)).map(s => ({
      id: s.id,
      name: s.name,
      label: s.name,
      description: s.description || 'Custom strategy',
      category: 'ai' as Category,
      tags: [s.is_template ? 'Template' : 'Custom'],
    })),
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
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <span className="text-sm font-bold text-white">Bot</span>
      </div>

      {/* Category filter */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-white/[0.06] overflow-x-auto scrollbar-none">
        {categories.map(c => (
          <button
            key={c.key}
            onClick={() => setCategory(c.key)}
            className={`px-2.5 py-1 rounded text-[11px] font-medium whitespace-nowrap ${
              category === c.key ? 'text-white bg-white/[0.08]' : 'text-slate-400 hover:text-white'
            }`}
          >
            {c.label}
          </button>
        ))}
      </div>

      {/* Bot cards */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {filtered.map((bot, i) => (
          <button
            key={i}
            onClick={() => setSelectedBot(bot)}
            className="w-full text-left p-3 rounded-lg bg-slate-800/50 border border-white/[0.04] hover:border-emerald-500/30 transition-colors"
          >
            <div className="flex items-start justify-between mb-1">
              <div>
                <span className="text-sm font-bold text-white">{bot.label}</span>
                <div className="flex items-center gap-1.5 mt-0.5">
                  {bot.tags.map((tag, ti) => (
                    <span
                      key={ti}
                      className={`text-[10px] px-1.5 py-0.5 rounded ${
                        tag === 'Beginner' ? 'bg-emerald-500/10 text-emerald-400' :
                        tag === 'Advanced' ? 'bg-purple-500/10 text-purple-400' :
                        tag === 'Bull Markets' ? 'bg-emerald-500/10 text-emerald-400' :
                        tag === 'Volatile Markets' ? 'bg-orange-500/10 text-orange-400' :
                        'bg-slate-500/10 text-slate-400'
                      }`}
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
              <div className="w-6 h-6 rounded-full bg-emerald-500/20 flex items-center justify-center">
                <span className="text-emerald-400 text-xs">&gt;</span>
              </div>
            </div>
            <p className="text-[11px] text-slate-400 mt-1 line-clamp-2">{bot.description}</p>
            {(bot.users || bot.profitPct) && (
              <div className="flex items-center gap-3 mt-2 text-[10px] text-slate-500">
                {bot.users && <span>{bot.users.toLocaleString()} users</span>}
                {bot.profitPct && <span className="text-emerald-400">+{bot.profitPct}%</span>}
              </div>
            )}
          </button>
        ))}
      </div>
    </div>
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
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSlModal, setShowSlModal] = useState(false);
  const [showTpModal, setShowTpModal] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [backtestChart, setBacktestChart] = useState<any>(null);

  // Quick backtest on mount
  useEffect(() => {
    const stratId = bot.id || strategies.find(s => s.name === bot.name)?.id;
    if (!stratId) return;
    api.futures.backtest.run({
      strategy_id: stratId,
      pairs: [pair],
      timeframe: '15m',
      timerange: '20240901-20241201',
      leverage,
      starting_balance: 1000,
    }).then(r => setBacktestChart(r)).catch(() => {});
  }, [bot, pair, leverage, strategies]);

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

  const metrics = backtestChart?.metrics;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-white/[0.06]">
        <button onClick={onBack} className="text-slate-400 hover:text-white text-sm">&lt;</button>
        <span className="text-sm font-bold text-white">{bot.label}</span>
      </div>

      {/* Leaderboard / Create tabs */}
      <div className="flex items-center border-b border-white/[0.06]">
        {['leaderboard', 'create'].map(t => (
          <button
            key={t}
            onClick={() => setViewTab(t as any)}
            className={`flex-1 py-2 text-xs font-medium capitalize border-b-2 ${
              viewTab === t ? 'text-white border-emerald-500' : 'text-slate-400 border-transparent'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {viewTab === 'leaderboard' && (
          <LeaderboardView botName={bot.label} pair={pair} />
        )}

        {viewTab === 'create' && (
          <div className="px-3 py-3 space-y-4">
            {/* Backtest mini chart */}
            {metrics && (
              <div className="bg-slate-800/50 rounded-lg p-3 border border-white/[0.04]">
                <div className="text-[10px] text-slate-500 mb-1">Backtest</div>
                <div className="h-20 bg-slate-900/50 rounded flex items-end px-1 gap-[1px]">
                  {(backtestChart?.equity_curve || []).slice(-40).map((val: number, i: number, arr: number[]) => {
                    const min = Math.min(...arr);
                    const max = Math.max(...arr);
                    const range = max - min || 1;
                    const h = ((val - min) / range) * 100;
                    return (
                      <div
                        key={i}
                        className="flex-1 bg-emerald-500/60 rounded-t"
                        style={{ height: `${Math.max(2, h)}%` }}
                      />
                    );
                  })}
                </div>
                <div className="flex justify-between mt-2 text-[10px]">
                  <span className="text-slate-500">Entry: {metrics.starting_balance}</span>
                  <span className="text-slate-500">Exit: {metrics.final_balance?.toFixed(0)}</span>
                  <span className={metrics.total_profit_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                    {metrics.total_profit_pct >= 0 ? '+' : ''}{metrics.total_profit_pct?.toFixed(2)}%
                  </span>
                </div>
              </div>
            )}

            {/* Margin / Leverage */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-slate-400">Margin</span>
                <select
                  value={leverage}
                  onChange={e => setLeverage(parseInt(e.target.value))}
                  className="bg-slate-800 border border-white/[0.06] rounded px-2 py-1 text-xs text-white outline-none"
                >
                  {[1, 2, 3, 5, 10, 20, 50, 75].map(l => (
                    <option key={l} value={l}>{l}x</option>
                  ))}
                </select>
              </div>
            </div>

            {/* Investment */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-white">Investment (Margin)</span>
                <span className="text-[10px] text-emerald-400">0 USDT</span>
              </div>
              <label className="text-[10px] text-slate-500">Available</label>
              <div className="flex items-center bg-slate-800 rounded-md border border-white/[0.06] mt-1">
                <input
                  type="number"
                  value={investment}
                  onChange={e => setInvestment(e.target.value)}
                  placeholder="Min: 1"
                  className="flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none"
                />
                <span className="text-xs text-slate-400 pr-2">USDT</span>
              </div>
              <div className="flex gap-1.5 mt-2">
                {['Min', '25%', '50%', '75%', '100%'].map(label => (
                  <button
                    key={label}
                    onClick={() => {
                      if (label === 'Min') setInvestment('1');
                      else setInvestment(String(Math.round(1000 * parseInt(label) / 100)));
                    }}
                    className="flex-1 py-1 rounded text-[10px] text-slate-400 bg-slate-800 border border-white/[0.06] hover:border-emerald-500/30 hover:text-white"
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
                <div className="mt-2 space-y-2">
                  {/* Drawdown Tolerance */}
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-slate-400">Drawdown Tolerance</span>
                    <span className="text-[11px] text-white">{drawdownTolerance}%</span>
                  </div>
                  <input
                    type="range"
                    min={10}
                    max={100}
                    value={drawdownTolerance}
                    onChange={e => setDrawdownTolerance(parseInt(e.target.value))}
                    className="w-full h-1 rounded-full appearance-none cursor-pointer bg-slate-700
                      [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3
                      [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-emerald-400"
                  />

                  {/* Stop-Loss */}
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-slate-400">Stop-Loss</span>
                    <button
                      onClick={() => setShowSlModal(true)}
                      className="text-[11px] text-slate-300 hover:text-white"
                    >
                      {stoploss ? `${stoploss}%` : 'Configure >'}
                    </button>
                  </div>

                  {/* Take-Profit */}
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
            className="w-full py-2.5 rounded-lg bg-emerald-500 text-white text-sm font-bold hover:bg-emerald-400 disabled:opacity-50"
          >
            {submitting ? 'Creating...' : 'Create'}
          </button>
        </div>
      )}

      {/* Stop-Loss Modal */}
      {showSlModal && (
        <ConfigModal
          title="Stop-Loss"
          value={stoploss}
          placeholder="1-99"
          suffix="%"
          onConfirm={v => { setStoploss(v); setShowSlModal(false); }}
          onReset={() => { setStoploss(''); setShowSlModal(false); }}
          onClose={() => setShowSlModal(false)}
        />
      )}

      {/* Take-Profit Modal */}
      {showTpModal && (
        <ConfigModal
          title="Take-Profit"
          value={takeprofit}
          placeholder="1-10000"
          suffix="%"
          onConfirm={v => { setTakeprofit(v); setShowTpModal(false); }}
          onReset={() => { setTakeprofit(''); setShowTpModal(false); }}
          onClose={() => setShowTpModal(false)}
        />
      )}
    </div>
  );
}

function ConfigModal({ title, value, placeholder, suffix, onConfirm, onReset, onClose }: {
  title: string; value: string; placeholder: string; suffix: string;
  onConfirm: (val: string) => void; onReset: () => void; onClose: () => void;
}) {
  const [val, setVal] = useState(value);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-[#1a1e2e] rounded-xl border border-white/[0.08] p-6 w-[380px] max-w-[90vw] shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold text-white">{title}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
        </div>

        <div className="flex items-center bg-slate-800 rounded-lg border border-white/[0.06] mb-6">
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

      {/* Placeholder leaderboard cards */}
      <div className="space-y-2">
        {[
          { pair: `${pair.split('/')[0]}USDT Perpetual`, leverage: '5x', profit: '+7.54%', yield24h: '+43.42%', runtime: '20d 14h', followers: 150 },
          { pair: 'KASUSDT Perpetual', leverage: '10x', profit: '+13.35%', yield24h: '+32.91%', runtime: '2d 0h', followers: 156 },
        ].map((item, i) => (
          <div key={i} className="p-3 rounded-lg bg-slate-800/50 border border-white/[0.04]">
            <div className="flex items-center justify-between mb-2">
              <div>
                <span className="text-xs font-bold text-white">{item.pair}</span>
                <span className="ml-2 text-[10px] text-slate-400">{item.leverage}</span>
              </div>
              <button className="px-3 py-1 rounded-full bg-emerald-500 text-white text-[10px] font-bold">
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
